"""调度器进程锁与崩溃恢复的安全回归测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from Agent.scheduler import (
    DEFAULT_JOB_TIMEOUT_SECONDS,
    NeedsApprovalError,
    SchedulerDaemon,
    SQLiteSchedulerStore,
    scheduler_status,
)
from Agent.storage import StateStore
from Agent.types import AgentResult, EventType, RunEvent


class _FakeTyperApp:
    """提供导入 CLI 所需的最小装饰器接口，不执行真实终端依赖。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """忽略展示参数；测试只需要保留被装饰函数本身。"""

        del args, kwargs

    def add_typer(self, *args: object, **kwargs: object) -> None:
        """模拟注册子命令，导入阶段无需保存命令树。"""

        del args, kwargs

    def command(self, *args: object, **kwargs: object):
        """返回不改写函数的命令装饰器。"""

        del args, kwargs

        def decorate(function):
            """原样返回命令函数，便于测试直接调用内部协程。"""

            return function

        return decorate

    def callback(self, *args: object, **kwargs: object):
        """复用命令装饰器模拟 Typer 根回调注册。"""

        return self.command(*args, **kwargs)


class _FakeConsole:
    """满足 CLI 模块级 Console 构造，测试不会实际渲染终端内容。"""

    def print(self, *args: object, **kwargs: object) -> None:
        """丢弃仅与终端展示相关的输出。"""

        del args, kwargs


class _FakeJSON:
    """模拟 Rich JSON 包装器的最小静态接口。"""

    @staticmethod
    def from_data(value: object) -> object:
        """直接返回输入，避免在逻辑测试中引入渲染行为。"""

        return value


class _FakeScheduledRuntime:
    """向 CLI runner 注入预先构造的 AgentResult，并记录调用参数。"""

    def __init__(self, result: AgentResult) -> None:
        """保存待返回结果和最后一次调用快照。"""

        self.result = result
        self.call: tuple[str, dict] | None = None

    async def run(self, task: str, **kwargs: object) -> AgentResult:
        """模拟 AgentRuntime.run 的异步聚合接口。"""

        self.call = (task, dict(kwargs))
        return self.result


def _load_scheduled_job_runner():
    """用最小 Typer/Rich/Web 替身加载 CLI 私有 runner，保持测试环境零第三方依赖。"""

    typer_module = types.ModuleType("typer")
    typer_module.Typer = _FakeTyperApp
    typer_module.Context = object
    typer_module.BadParameter = ValueError

    def parameter(default: object = None, *args: object, **kwargs: object) -> object:
        """让 Typer Option/Argument 在导入时保留原始默认值。"""

        del args, kwargs
        return default

    typer_module.Option = parameter
    typer_module.Argument = parameter

    rich_module = types.ModuleType("rich")
    rich_module.__path__ = []
    rich_console_module = types.ModuleType("rich.console")
    rich_console_module.Console = _FakeConsole
    rich_json_module = types.ModuleType("rich.json")
    rich_json_module.JSON = _FakeJSON
    web_module = types.ModuleType("run_ui.web")
    web_module.create_app = None
    module_name = "run_ui._scheduler_cli_test"
    cli_path = Path(__file__).resolve().parents[1] / "run_ui" / "cli.py"
    specification = importlib.util.spec_from_file_location(module_name, cli_path)
    if specification is None or specification.loader is None:
        raise RuntimeError("无法加载 run_ui/cli.py 测试模块")
    module = importlib.util.module_from_spec(specification)
    replacements = {
        "typer": typer_module,
        "rich": rich_module,
        "rich.console": rich_console_module,
        "rich.json": rich_json_module,
        "run_ui.web": web_module,
        module_name: module,
    }
    with patch.dict(sys.modules, replacements):
        specification.loader.exec_module(module)
    return module._run_scheduled_job


def _scheduled_job_payload() -> dict:
    """返回 CLI runner 所需的最小、无工具能力包任务记录。"""

    return {
        "prompt": "执行后台测试",
        "session_id": None,
        "capability_json": json.dumps({"tools": []}),
    }


async def _unused_runner(job: dict) -> None:
    """满足守护进程构造协议；进程锁测试不会实际执行任务。"""

    del job


async def _slow_runner(job: dict) -> None:
    """模拟永久等待的外部调用，供守护进程超时与取消路径测试。"""

    del job
    await asyncio.sleep(60)


async def _approval_runner(job: dict) -> None:
    """模拟能力包越界等必须由用户重新审批的后台执行结果。"""

    del job
    raise NeedsApprovalError("能力包不包含目标路径")


class SchedulerSecurityTests(unittest.TestCase):
    """验证单实例锁采用 fail-closed 语义，崩溃任务不会被静默重跑。"""

    def setUp(self) -> None:
        """为每个用例创建独立状态目录和 SQLite 数据库。"""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = StateStore(self.root / "state.db")
        self.schedules = SQLiteSchedulerStore(self.store)

    def tearDown(self) -> None:
        """关闭临时数据库所在目录，避免测试之间共享 PID 文件。"""

        self.temporary.cleanup()

    def _running_job(self, *, recurring: bool) -> dict:
        """创建任务、原子标记为 running，并返回执行所需的数据库快照。"""

        schedule_id = self.schedules.add_schedule(
            {
                "cron": "* * * * *",
                "prompt": "执行安全回归任务",
                "timezone": "UTC",
                "recurring": recurring,
            }
        )
        job = self.store.query("SELECT * FROM schedules WHERE id=?", (schedule_id,))[0]
        self.assertTrue(self.schedules.mark_running(schedule_id))
        return job

    def test_pid_lock_is_exclusive(self) -> None:
        """第一个守护进程持锁时，第二个实例必须立即拒绝启动。"""

        first = SchedulerDaemon(self.schedules, _unused_runner, self.root)
        second = SchedulerDaemon(self.schedules, _unused_runner, self.root)
        first._acquire()
        try:
            with self.assertRaises(RuntimeError):
                second._acquire()
            self.assertEqual(first.pid_file.read_text(encoding="ascii"), str(os.getpid()))
        finally:
            first.pid_file.unlink(missing_ok=True)

    def test_unknown_pid_state_keeps_lock(self) -> None:
        """系统无法判断 PID 状态时应保留锁并安全失败，不能当作陈旧文件删除。"""

        pid_file = self.root / "scheduler.pid"
        pid_file.write_text("99123", encoding="ascii")
        daemon = SchedulerDaemon(self.schedules, _unused_runner, self.root)
        with patch("Agent.scheduler._pid_is_alive", side_effect=OSError("query denied")):
            with self.assertRaises(RuntimeError):
                daemon._acquire()
            status = scheduler_status(self.root)
        self.assertTrue(status["running"])
        self.assertFalse(status["verified"])
        self.assertEqual(pid_file.read_text(encoding="ascii"), "99123")

    def test_confirmed_dead_pid_can_be_replaced(self) -> None:
        """只有明确确认旧 PID 已死亡时，新的守护进程才可以原子接管锁。"""

        pid_file = self.root / "scheduler.pid"
        pid_file.write_text("99123", encoding="ascii")
        daemon = SchedulerDaemon(self.schedules, _unused_runner, self.root)
        with patch("Agent.scheduler._pid_is_alive", return_value=False):
            daemon._acquire()
        try:
            self.assertEqual(pid_file.read_text(encoding="ascii"), str(os.getpid()))
        finally:
            pid_file.unlink(missing_ok=True)

    def test_crashed_running_job_requires_approval(self) -> None:
        """运行租约过期后解除卡死，但不得自动重复执行未知结果的副作用任务。"""

        schedule_id = self.schedules.add_schedule(
            {"cron": "* * * * *", "prompt": "检查任务", "timezone": "UTC"}
        )
        self.assertTrue(self.schedules.mark_running(schedule_id))
        now = datetime.now(timezone.utc)
        old = (now - timedelta(minutes=6)).isoformat()
        self.store.execute("UPDATE schedules SET last_run=? WHERE id=?", (old, schedule_id))

        self.assertEqual(self.schedules.due(now), [])
        row = self.store.query("SELECT status FROM schedules WHERE id=?", (schedule_id,))[0]
        self.assertEqual(row["status"], "needs_approval")

    def test_stale_recovery_excludes_active_schedule_ids(self) -> None:
        """同样超过租约的任务中，仅非本进程活跃 ID 可以被恢复为待审批。"""

        active_job = self._running_job(recurring=True)
        abandoned_job = self._running_job(recurring=True)
        now = datetime.now(timezone.utc)
        old = (now - timedelta(minutes=6)).isoformat()
        self.store.execute(
            "UPDATE schedules SET last_run=? WHERE id IN (?,?)",
            (old, active_job["id"], abandoned_job["id"]),
        )

        self.assertEqual(
            self.schedules.due(now, active_schedule_ids={active_job["id"]}),
            [],
        )

        rows = self.store.query(
            "SELECT id,status FROM schedules WHERE id IN (?,?)",
            (active_job["id"], abandoned_job["id"]),
        )
        statuses = {row["id"]: row["status"] for row in rows}
        self.assertEqual(statuses[active_job["id"]], "running")
        self.assertEqual(statuses[abandoned_job["id"]], "needs_approval")

    def test_daemon_keeps_own_long_running_job_out_of_stale_recovery(self) -> None:
        """守护循环后续轮询必须携带活跃集合，长任务超过五分钟仍保持 running。"""

        schedule_id = self.schedules.add_schedule(
            {
                "cron": "* * * * *",
                "prompt": "执行长任务",
                "timezone": "UTC",
                "recurring": True,
            }
        )
        self.store.execute(
            "UPDATE schedules SET next_run=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), schedule_id),
        )

        async def scenario() -> None:
            """启动真实轮询循环，在 runner 阻塞期间人工老化其 last_run。"""

            started = asyncio.Event()
            release = asyncio.Event()

            async def long_runner(job: dict) -> None:
                """保持任务活跃，直到测试确认第二次轮询没有误回收。"""

                self.assertEqual(job["id"], schedule_id)
                started.set()
                await release.wait()

            daemon = SchedulerDaemon(
                self.schedules,
                long_runner,
                self.root,
                job_timeout_seconds=5,
            )
            daemon_task = asyncio.create_task(daemon.run_forever())
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
                self.store.execute(
                    "UPDATE schedules SET last_run=? WHERE id=?",
                    (old, schedule_id),
                )
                # 守护进程每秒轮询一次；等待越过下一轮以覆盖 active IDs 传递路径。
                await asyncio.sleep(1.2)
                row = self.store.query(
                    "SELECT status FROM schedules WHERE id=?",
                    (schedule_id,),
                )[0]
                self.assertEqual(row["status"], "running")
                self.assertIn(schedule_id, daemon._active_schedule_ids)
            finally:
                release.set()
                daemon.stop()
                await asyncio.wait_for(daemon_task, timeout=3)
            self.assertEqual(daemon._active_schedule_ids, set())

        asyncio.run(scenario())
        row = self.store.query("SELECT status FROM schedules WHERE id=?", (schedule_id,))[0]
        self.assertEqual(row["status"], "ready")

    def test_mark_complete_does_not_overwrite_external_status(self) -> None:
        """完成回调到达较晚时，不得覆盖人工审批或其他管理操作写入的新状态。"""

        for recurring, error in ((False, None), (True, "执行失败"), (True, None)):
            with self.subTest(recurring=recurring, error=error):
                job = self._running_job(recurring=recurring)
                self.store.execute(
                    "UPDATE schedules SET status='needs_approval' WHERE id=?",
                    (job["id"],),
                )
                expected = self.store.query(
                    "SELECT * FROM schedules WHERE id=?",
                    (job["id"],),
                )[0]

                self.schedules.mark_complete(job, error=error)

                actual = self.store.query(
                    "SELECT * FROM schedules WHERE id=?",
                    (job["id"],),
                )[0]
                self.assertEqual(actual, expected)

    def test_job_timeout_is_finite_and_marks_one_shot_failed(self) -> None:
        """永久挂起的 runner 必须在配置上限内取消，并把一次性任务标记为失败。"""

        job = self._running_job(recurring=False)
        daemon = SchedulerDaemon(
            self.schedules,
            _slow_runner,
            self.root,
            job_timeout_seconds=0.01,
        )

        asyncio.run(daemon._run_job(job))

        row = self.store.query("SELECT enabled,status FROM schedules WHERE id=?", (job["id"],))[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["enabled"], 0)

    def test_timeout_configuration_rejects_unbounded_values(self) -> None:
        """默认超时必须有限，零、负数、无穷大和 NaN 均应在启动前被拒绝。"""

        default_daemon = SchedulerDaemon(self.schedules, _unused_runner, self.root)
        self.assertEqual(default_daemon.job_timeout_seconds, DEFAULT_JOB_TIMEOUT_SECONDS)
        self.assertTrue(math.isfinite(default_daemon.job_timeout_seconds))
        self.assertGreater(default_daemon.job_timeout_seconds, 0)
        for invalid in (0, -1, math.inf, -math.inf, math.nan):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    SchedulerDaemon(
                        self.schedules,
                        _unused_runner,
                        self.root,
                        job_timeout_seconds=invalid,
                    )

    def test_needs_approval_signal_pauses_without_retry(self) -> None:
        """审批信号应停在 needs_approval，不能被周期任务逻辑转换成自动重试。"""

        job = self._running_job(recurring=True)
        original_next_run = job["next_run"]
        daemon = SchedulerDaemon(self.schedules, _approval_runner, self.root)

        asyncio.run(daemon._run_job(job))

        row = self.store.query(
            "SELECT enabled,status,retries,next_run FROM schedules WHERE id=?",
            (job["id"],),
        )[0]
        self.assertEqual(row["status"], "needs_approval")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["retries"], 0)
        self.assertEqual(row["next_run"], original_next_run)

    def test_cli_runner_rejects_incomplete_runtime_result(self) -> None:
        """即使事件流正常结束，completed=False 也必须作为调度失败抛出。"""

        runner = _load_scheduled_job_runner()
        runtime = _FakeScheduledRuntime(
            AgentResult("session-1", "已达到最大执行轮数", False, [])
        )

        with self.assertRaisesRegex(RuntimeError, "未完成任务"):
            asyncio.run(runner(runtime, _scheduled_job_payload()))
        self.assertEqual(runtime.call[0], "执行后台测试")

    def test_cli_runner_rejects_error_event_even_if_completed(self) -> None:
        """ERROR 事件优先于 completed 标志，防止异常后伪 FINAL 被误判成功。"""

        runner = _load_scheduled_job_runner()
        event = RunEvent(
            EventType.ERROR,
            "session-2",
            {"error": "Provider 连接失败"},
        )
        runtime = _FakeScheduledRuntime(
            AgentResult("session-2", "看似完成", True, [event])
        )

        with self.assertRaisesRegex(RuntimeError, "Provider 连接失败"):
            asyncio.run(runner(runtime, _scheduled_job_payload()))

    def test_cli_runner_promotes_approval_payload_to_typed_signal(self) -> None:
        """任意 Runtime 事件的 needs_approval=true 都应升级为明确审批异常。"""

        runner = _load_scheduled_job_runner()
        event = RunEvent(
            EventType.HOOK,
            "session-3",
            {"needs_approval": True, "reason": "固定插件版本已变化"},
        )
        runtime = _FakeScheduledRuntime(
            AgentResult("session-3", "", False, [event])
        )

        with self.assertRaisesRegex(NeedsApprovalError, "固定插件版本已变化"):
            asyncio.run(runner(runtime, _scheduled_job_payload()))

    def test_cli_runner_promotes_tool_metadata_approval_signal(self) -> None:
        """工具失败事件嵌套在 metadata 的审批标志也必须暂停后台任务。"""

        runner = _load_scheduled_job_runner()
        event = RunEvent(
            EventType.TOOL_FAILED,
            "session-tool",
            {
                "content": "子代理工具白名单不包含该工具",
                "metadata": {"needs_approval": True},
            },
        )
        runtime = _FakeScheduledRuntime(
            AgentResult("session-tool", "模型随后结束", True, [event])
        )

        with self.assertRaisesRegex(NeedsApprovalError, "工具白名单"):
            asyncio.run(runner(runtime, _scheduled_job_payload()))

    def test_cli_runner_promotes_capability_denial_to_approval(self) -> None:
        """兼容旧 Runtime：后台能力包拒绝事件即使无标志也必须暂停等待审批。"""

        runner = _load_scheduled_job_runner()
        event = RunEvent(
            EventType.APPROVAL_RESOLVED,
            "session-4",
            {"allowed": False, "reason": "后台能力包不包含该调用"},
        )
        runtime = _FakeScheduledRuntime(
            AgentResult("session-4", "模型随后返回了文本", True, [event])
        )

        with self.assertRaisesRegex(NeedsApprovalError, "后台能力包"):
            asyncio.run(runner(runtime, _scheduled_job_payload()))


if __name__ == "__main__":
    # 允许直接运行本文件，同时保持被 unittest discover 导入时没有额外副作用。
    unittest.main()
