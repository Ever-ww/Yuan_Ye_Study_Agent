"""SQLite 持久 Cron 与单实例本地调度守护进程。

计划使用标准五字段 Cron 和显式时区。持久记录包含创建时冻结的能力包；后台 runner
执行越界调用时只能进入待审批/失败状态，不能自动扩大权限。守护进程通过原子状态
更新防止同一任务重叠，并以 PID/停止标记文件维持本机单实例。
"""

from __future__ import annotations

import asyncio
import errno
import json
import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

# ``croniter`` 提供完整解析；未安装时使用本模块的五字段子集实现安全降级。
try:
    from croniter import croniter
except ImportError:
    croniter = None

from .permissions import CapabilityGrant
from .storage import StateStore
from .types import utc_now


JobRunner = Callable[[dict[str, Any]], Awaitable[None]]


class NeedsApprovalError(RuntimeError):
    """表示后台任务需要人工扩权或重新确认，不能作为普通故障自动重试。

    runner 可在能力包越界、固定插件内容哈希变化等情况下抛出该异常。守护进程只消费其
    类型，不解析错误文案，从而避免依赖容易变化的自然语言字符串决定安全状态。
    """

# 守护进程如果在任务处于 ``running`` 时异常退出，数据库中不会再有进程负责收尾。
# 五分钟租约到期后把任务转入人工确认，而不是自动重跑；这样既能解除永久卡死，又不会在
# 无法判断上一次副作用是否完成时制造重复执行。
RUNNING_LEASE = timedelta(minutes=5)

# 单次后台任务不能无限占用 ``running`` 状态。十五分钟既给多轮模型/工具调用留出空间，
# 又能在 Provider、Hook 或第三方集成永久挂起时，于可预期时间内进入失败/重试流程。
DEFAULT_JOB_TIMEOUT_SECONDS = 15 * 60.0


@dataclass(frozen=True)
class Schedule:
    """面向 API 的不可变计划摘要；数据库记录还包含能力包与重试状态。"""

    id: str
    cron: str
    prompt: str
    timezone: str
    recurring: bool
    enabled: bool
    status: str
    next_run: str | None
    expires_at: str | None


class SQLiteSchedulerStore:
    """对 ``schedules`` 表执行校验、状态迁移和到期判断。"""

    def __init__(self, store: StateStore) -> None:
        """绑定共享 SQLite 状态库。"""

        self.store = store

    def add_schedule(self, record: dict[str, Any]) -> str:
        """验证五字段表达式、时区和能力包后创建计划。

        周期任务若未明确永久有效，默认七天到期；这限制了被遗忘的后台权限长期运行。
        ``next_run`` 统一转换为 UTC 存储，展示或计算下次触发时再使用配置时区。
        """

        expression = str(record["cron"])
        if not _cron_valid(expression):
            raise ValueError("Cron 必须是有效的五字段表达式")
        if len(expression.split()) != 5:
            raise ValueError("只支持标准五字段 Cron")
        tz_name = str(record.get("timezone", "Asia/Shanghai"))
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        schedule_id = uuid4().hex[:8]
        recurring = bool(record.get("recurring", True))
        next_run = _cron_next(expression, now).astimezone(timezone.utc).isoformat()
        expires_at = record.get("expires_at")
        if recurring and expires_at is None and not record.get("no_expiry", False):
            expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        grant = record.get("capability") or CapabilityGrant()
        grant_json = json.dumps(grant.to_dict() if isinstance(grant, CapabilityGrant) else grant, ensure_ascii=False)
        self.store.execute(
            "INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                schedule_id, expression, str(record["prompt"]), tz_name, int(recurring), 1, "ready",
                next_run, None, expires_at, grant_json, record.get("session_id"), 0, utc_now(),
            ),
        )
        return schedule_id

    def list_schedules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """按创建时间列出全部或仅启用的计划记录。"""

        sql = "SELECT * FROM schedules" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY created_at"
        return self.store.query(sql)

    def delete(self, schedule_id: str) -> bool:
        """删除存在的计划并报告是否命中，便于 CLI 给出准确反馈。"""

        if not self.store.query("SELECT id FROM schedules WHERE id=?", (schedule_id,)):
            return False
        self.store.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        return True

    def due(
        self,
        now: datetime | None = None,
        active_schedule_ids: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """返回当前应运行任务，并原地处理到期和严重错过的一次性任务。

        周期任务由下一次计算自然跳过重复错过周期；一次性任务错过超过五分钟会转为
        ``needs_approval``，避免机器长时间关机后突然执行过时操作。当前守护进程仍在
        执行的 ID 通过 ``active_schedule_ids`` 排除，长任务不会被固定租约误判为崩溃残留。
        """

        current = now or datetime.now(timezone.utc)
        current = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)
        self.recover_stale_running(current, active_schedule_ids=active_schedule_ids)
        instant = current.isoformat()
        rows = self.store.query(
            "SELECT * FROM schedules WHERE enabled=1 AND status='ready' AND next_run IS NOT NULL AND next_run<=?",
            (instant,),
        )
        due: list[dict[str, Any]] = []
        for row in rows:
            if row.get("expires_at") and datetime.fromisoformat(str(row["expires_at"])) <= current:
                self.store.execute("UPDATE schedules SET enabled=0,status='expired' WHERE id=?", (row["id"],))
                continue
            scheduled = datetime.fromisoformat(str(row["next_run"]))
            if not bool(row["recurring"]) and current - scheduled > timedelta(minutes=5):
                self.store.execute("UPDATE schedules SET status='needs_approval' WHERE id=?", (row["id"],))
                continue
            due.append(row)
        return due

    def recover_stale_running(
        self,
        now: datetime | None = None,
        active_schedule_ids: Iterable[str] = (),
    ) -> int:
        """把租约过期的运行中任务转为 ``needs_approval``，并返回恢复数量。

        守护进程崩溃时无法证明任务究竟“尚未开始”还是“副作用已完成但状态未提交”，因此
        这里采用保守恢复：不自动重新排队，只解除永久 ``running`` 并等待用户决定。
        ``active_schedule_ids`` 是调用方持有的本进程活跃快照；这些任务即使超过固定租约，
        也仍有协程负责收尾，不能由轮询线程提前改成待审批。
        """

        current = now or datetime.now(timezone.utc)
        current = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)
        cutoff = (current - RUNNING_LEASE).isoformat()
        active = {str(schedule_id) for schedule_id in active_schedule_ids}
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id FROM schedules "
                "WHERE status='running' AND last_run IS NOT NULL AND last_run<=?",
                (cutoff,),
            ).fetchall()
            recovered = 0
            # 逐条使用条件 UPDATE，避免活跃集合过大时触发 SQLite 参数数量上限；条件同时
            # 防止状态在 SELECT 后被其他管理操作改变时遭到覆盖。
            for row in rows:
                schedule_id = str(row["id"])
                if schedule_id in active:
                    continue
                cursor = db.execute(
                    "UPDATE schedules SET status='needs_approval' "
                    "WHERE id=? AND status='running' AND last_run IS NOT NULL AND last_run<=?",
                    (schedule_id, cutoff),
                )
                recovered += cursor.rowcount
            return recovered

    def mark_running(self, schedule_id: str) -> bool:
        """使用条件 UPDATE 原子抢占任务，阻止多个守护进程重复运行。"""

        with self.store.connection() as db:
            cursor = db.execute("UPDATE schedules SET status='running',last_run=? WHERE id=? AND status='ready'", (utc_now(), schedule_id))
            return cursor.rowcount == 1

    def mark_needs_approval(self, schedule_id: str) -> None:
        """把已抢占任务暂停在人工审批状态，且不增加自动重试计数。"""

        self.store.execute(
            "UPDATE schedules SET status='needs_approval' WHERE id=? AND status='running'",
            (schedule_id,),
        )

    def mark_complete(self, job: dict[str, Any], *, error: str | None = None) -> None:
        """完成状态迁移：一次性终止，周期任务重试或计算下次运行。

        周期错误最多重试三次，退避间隔按 60、120、240 秒增长；重试耗尽后跳到
        下一正常 Cron 周期并清零计数，避免永久卡在 ``running``。每条终态 UPDATE 都
        要求记录仍为 ``running``；若人工审批或管理操作已先改变状态，迟到 runner 无权
        把该外部决定覆盖成 completed、failed 或下一轮 ready。
        """

        if not bool(job["recurring"]):
            self.store.execute(
                "UPDATE schedules SET enabled=0,status=? WHERE id=? AND status='running'",
                ("failed" if error else "completed", job["id"]),
            )
            return
        if error and int(job["retries"]) < 3:
            retry = datetime.now(timezone.utc) + timedelta(seconds=2 ** (int(job["retries"]) + 1) * 30)
            self.store.execute(
                "UPDATE schedules SET status='ready',next_run=?,retries=retries+1 "
                "WHERE id=? AND status='running'",
                (retry.isoformat(), job["id"]),
            )
            return
        tz = ZoneInfo(str(job["timezone"]))
        next_run = _cron_next(str(job["cron"]), datetime.now(tz)).astimezone(timezone.utc).isoformat()
        self.store.execute(
            "UPDATE schedules SET status='ready',next_run=?,retries=0 "
            "WHERE id=? AND status='running'",
            (next_run, job["id"]),
        )


class SchedulerDaemon:
    """每秒轮询到期任务并并发执行的单实例异步守护进程。"""

    def __init__(
        self,
        store: SQLiteSchedulerStore,
        runner: JobRunner,
        state_dir: Path,
        *,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    ) -> None:
        """配置存储、执行回调、有限任务超时和进程标记文件。

        ``job_timeout_seconds`` 必须是大于零的有限秒数。把超时放在守护进程边界，能让
        所有 runner 实现共享相同的终止语义；构造阶段即拒绝无穷大、NaN 和非正值，避免
        一次配置错误悄悄恢复成“永不超时”。本方法只保存配置，不立即占用单实例锁。
        """

        timeout = float(job_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("job_timeout_seconds 必须是大于零的有限秒数")
        self.store = store
        self.runner = runner
        self.job_timeout_seconds = timeout
        self.pid_file = state_dir / "scheduler.pid"
        self.stop_file = state_dir / "scheduler.stop"
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_schedule_ids: set[str] = set()
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        """取得单实例标记后持续调度，停止时等待在途任务并清理 PID。"""

        self._acquire()
        try:
            while not self._stop.is_set():
                if self.stop_file.exists():
                    self.stop_file.unlink(missing_ok=True)
                    self._stop.set()
                    continue
                # 传入不可变快照，避免长任务超过 RUNNING_LEASE 后被同一守护进程误回收。
                for job in self.store.due(active_schedule_ids=frozenset(self._active_schedule_ids)):
                    if self.store.mark_running(job["id"]):
                        schedule_id = str(job["id"])
                        self._active_schedule_ids.add(schedule_id)
                        task = asyncio.create_task(self._run_job(job), name=f"cron-{job['id']}")
                        self._tasks.add(task)
                        # 保存强引用直到任务结束；回调完成后自动回收集合成员。
                        task.add_done_callback(self._tasks.discard)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
        finally:
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            # 仅删除仍由本进程持有的标记；若管理员已经替换文件，不应误删新守护进程的锁。
            _unlink_if_unchanged(self.pid_file, str(os.getpid()))

    def stop(self) -> None:
        """请求当前进程内的循环在下一轮安全停止。"""

        self._stop.set()

    async def _run_job(self, job: dict[str, Any]) -> None:
        """在有限时间内执行 runner，并保证任务总能离开 ``running`` 状态。

        超时和普通异常都转换为 ``mark_complete`` 的错误参数，使一次性任务进入
        ``failed``，周期任务进入既有的有限重试流程。这里不把异常重新抛到守护循环，
        避免一个任务终止其他并发任务。
        """

        schedule_id = str(job["id"])
        error = None
        try:
            try:
                await asyncio.wait_for(self.runner(job), timeout=self.job_timeout_seconds)
            except NeedsApprovalError:
                # 审批缺失不是瞬时技术错误；直接重试既无助于成功，也可能反复触发副作用。
                self.store.mark_needs_approval(schedule_id)
                return
            except asyncio.TimeoutError:
                error = f"任务执行超过 {self.job_timeout_seconds:g} 秒"
            except Exception as exc:
                # 某些异常的字符串为空；保留类型名可避免调度记录看起来像无缘由失败。
                error = str(exc) or type(exc).__name__
            self.store.mark_complete(job, error=error)
        finally:
            # 包括状态库写入失败和协程取消在内，都不能让本地活跃集合永久残留该 ID。
            self._active_schedule_ids.discard(schedule_id)

    def _acquire(self) -> None:
        """用独占创建原子取得 PID 锁；无法确认陈旧时一律安全失败。"""

        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        own_pid = str(os.getpid())
        # “检查后写入”存在两个进程同时通过检查的竞态；O_EXCL 把判断和创建合并为一个
        # 文件系统原子操作。循环只用于清理已确认死亡或格式损坏的旧锁。
        for _ in range(8):
            try:
                descriptor = os.open(
                    self.pid_file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                try:
                    raw_pid = self.pid_file.read_text(encoding="ascii").strip()
                except FileNotFoundError:
                    # 文件在独占创建失败后被另一方删除，重新竞争即可。
                    continue
                try:
                    pid = int(raw_pid)
                except ValueError:
                    _unlink_if_unchanged(self.pid_file, raw_pid)
                    continue
                try:
                    alive = _pid_is_alive(pid)
                except OSError as exc:
                    # 未知系统错误不能证明旧进程已死亡，保留锁并拒绝启动。
                    raise RuntimeError(f"无法确认 scheduler PID={pid} 的状态：{exc}") from exc
                if alive:
                    raise RuntimeError(f"scheduler 已运行，PID={pid}")
                _unlink_if_unchanged(self.pid_file, raw_pid)
                continue
            else:
                try:
                    os.write(descriptor, own_pid.encode("ascii"))
                finally:
                    os.close(descriptor)
                self.stop_file.unlink(missing_ok=True)
                return
        raise RuntimeError("scheduler PID 锁被持续并发修改，已拒绝启动")


def scheduler_status(state_dir: Path) -> dict[str, Any]:
    """读取 PID 并探测进程；仅在能够证明进程死亡时清除陈旧标记。"""

    pid_file = state_dir / "scheduler.pid"
    if not pid_file.exists():
        return {"running": False}
    try:
        raw_pid = pid_file.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return {"running": False}
    try:
        pid = int(raw_pid)
    except ValueError:
        _unlink_if_unchanged(pid_file, raw_pid)
        return {"running": False}
    try:
        alive = _pid_is_alive(pid)
    except OSError as exc:
        # 权限不足或未知平台错误都不能作为删除其他进程锁文件的依据。
        return {"running": True, "pid": pid, "verified": False, "reason": str(exc)}
    if alive:
        return {"running": True, "pid": pid, "verified": True}
    _unlink_if_unchanged(pid_file, raw_pid)
    return {"running": False}


def _unlink_if_unchanged(path: Path, expected: str) -> bool:
    """仅当 PID 文件内容仍等于预期值时删除，避免清理并发创建的新锁。"""

    try:
        current = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return False
    if current != expected:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _pid_is_alive(pid: int) -> bool:
    """跨平台、无副作用地判断 PID 是否仍存活。

    POSIX 使用信号 0；Windows 不能调用 ``os.kill(pid, 0)``，因为该平台会把普通
    数值信号交给 ``TerminateProcess``，有误杀目标进程的风险，所以改用只查询句柄。
    权限不足表示进程存在但不可查询，按“仍存活”处理；只有明确的“不存在”才返回假。
    """

    if pid <= 0:
        return False
    if os.name == "nt":
        # 延迟导入保持非 Windows 平台无需加载 ctypes 的 Win32 定义。
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 5:  # ERROR_ACCESS_DENIED：进程存在，但当前用户不能查询。
                return True
            if error == 87:  # ERROR_INVALID_PARAMETER：PID 不存在。
                return False
            raise OSError(error, f"OpenProcess({pid}) 失败")
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                error = ctypes.get_last_error()
                raise OSError(error, f"GetExitCodeProcess({pid}) 失败")
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        raise
    return True


def _cron_valid(expression: str) -> bool:
    """使用 croniter 或内置解析器验证标准五字段表达式。"""

    if croniter is not None:
        return bool(croniter.is_valid(expression))
    try:
        fields = expression.split()
        if len(fields) != 5:
            return False
        for value, minimum, maximum in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)):
            _expand_field(value, minimum, maximum)
        return True
    except ValueError:
        return False


def _expand_field(value: str, minimum: int, maximum: int) -> set[int]:
    """展开单个 Cron 字段的列表、区间、通配符和步长。"""

    result: set[int] = set()
    for item in value.split(","):
        base, slash, step_value = item.partition("/")
        step = int(step_value) if slash else 1
        if step < 1:
            raise ValueError("cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError("cron field out of range")
        result.update(range(start, end + 1, step))
    return result


def _cron_next(expression: str, base: datetime) -> datetime:
    """计算严格晚于 ``base`` 的下一次触发时间。

    内置实现逐分钟搜索且最多两年，避免无效组合造成无限循环；星期日同时接受 Cron
    约定的 0 和 7。日与星期字段遵循常见 Cron 的受限字段 OR 语义。
    """

    if croniter is not None:
        return croniter(expression, base).get_next(datetime)
    minute, hour, day, month, weekday = [
        _expand_field(value, minimum, maximum)
        for value, minimum, maximum in zip(expression.split(), (0, 0, 1, 1, 0), (59, 23, 31, 12, 7))
    ]
    candidate = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 366 * 2):
        cron_weekday = (candidate.weekday() + 1) % 7
        weekday_match = cron_weekday in weekday or (cron_weekday == 0 and 7 in weekday)
        day_all = len(day) == 31
        weekday_all = len(weekday) >= 7
        day_match = candidate.day in day
        calendar_match = day_match and weekday_match if day_all or weekday_all else day_match or weekday_match
        if candidate.minute in minute and candidate.hour in hour and candidate.month in month and calendar_match:
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("无法在两年内找到下一次 Cron 时间")
