"""固定间隔、单次时间与标准五段 Cron 的下一次时间计算。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from croniter.croniter import CroniterBadCronError, CroniterBadDateError
from tzlocal import get_localzone_name

from .models import CronPreview, CronSchedule, parse_time, utc_iso


class CronScheduleCalculator:
    """以 UTC 保存绝对时间，以 Job 的 IANA 时区解释五段表达式。"""

    @staticmethod
    def local_timezone() -> str:
        return get_localzone_name()

    def validate(self, expression: str, timezone_name: str) -> CronSchedule:
        expression = expression.strip()
        if expression.startswith("@") or len(expression.split()) != 5:
            raise ValueError("Cron 表达式必须是标准五段格式，不支持快捷别名、秒或年份字段")
        _validate_standard_fields(expression)
        zone = _zone(timezone_name)
        try:
            if not croniter.is_valid(expression, second_at_beginning=False):
                raise ValueError("Cron 表达式无效")
            croniter(expression, datetime.now(zone), day_or=True).get_next(datetime)
        except (CroniterBadCronError, CroniterBadDateError, ValueError) as exc:
            raise ValueError(f"Cron 表达式无效：{expression}") from exc
        return CronSchedule(kind="cron", expression=expression, timezone=timezone_name)

    def next_after(self, schedule: CronSchedule, base_time: datetime) -> datetime | None:
        base = _aware_utc(base_time)
        if schedule.kind == "once":
            target = parse_time(str(schedule.run_at))
            return target if target > base else None
        if schedule.kind == "interval":
            return base + timedelta(seconds=int(schedule.interval_seconds or 0))
        expression = str(schedule.expression)
        zone = _zone(schedule.timezone)
        # croniter 对带时区 datetime 会把春季不存在的墙上时间平移到 03:00。
        # 这里刻意以 naive 墙上时间枚举，再由 ZoneInfo 严格验证，从而真正跳过该周期。
        local_base = base.astimezone(zone).replace(tzinfo=None)
        try:
            iterator = croniter(expression, local_base, day_or=True)
            for _ in range(10_000):
                candidate = iterator.get_next(datetime)
                normalized = _normalize_local_candidate(candidate, zone)
                if normalized is not None and normalized.astimezone(timezone.utc) > base:
                    return normalized.astimezone(timezone.utc)
        except (CroniterBadCronError, CroniterBadDateError) as exc:
            raise ValueError(f"无法计算 Cron 下一次执行时间：{expression}") from exc
        raise ValueError("Cron 表达式在可计算范围内没有下一个合法时间")

    def next_future(
        self,
        schedule: CronSchedule,
        scheduled_at: datetime,
        now: datetime,
    ) -> datetime | None:
        """沿原计划时间轴推进到严格晚于 now，避免停机补跑风暴。"""
        selected = _aware_utc(scheduled_at)
        current = _aware_utc(now)
        if schedule.kind == "once":
            return None
        if schedule.kind == "interval":
            seconds = int(schedule.interval_seconds or 0)
            missed = max(1, int((current - selected).total_seconds() // seconds) + 1)
            return selected + timedelta(seconds=seconds * missed)
        candidate = selected
        for _ in range(100_000):
            next_value = self.next_after(schedule, candidate)
            if next_value is None or next_value > current:
                return next_value
            candidate = next_value
        raise ValueError("Cron 错过周期数量超过安全上限")

    def preview(
        self,
        schedule: CronSchedule,
        *,
        count: int = 5,
        base_time: datetime | None = None,
    ) -> CronPreview:
        if count < 1 or count > 20:
            raise ValueError("预览数量必须位于 1 到 20 之间")
        selected = _aware_utc(base_time or datetime.now(timezone.utc))
        values: list[str] = []
        for _ in range(count):
            next_value = self.next_after(schedule, selected)
            if next_value is None:
                break
            values.append(utc_iso(next_value))
            selected = next_value
        return CronPreview(schedule=schedule, next_runs=tuple(values))


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("调度基准时间必须包含时区")
    return value.astimezone(timezone.utc)


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知 IANA 时区：{value}") from exc


def _normalize_local_candidate(value: datetime, zone: ZoneInfo) -> datetime | None:
    """跳过不存在的墙上时间，并固定重复时间只使用 fold=0。"""
    naive = value.replace(tzinfo=None)
    first = naive.replace(tzinfo=zone, fold=0)
    round_trip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if round_trip != naive:
        return None
    return first


def _validate_standard_fields(expression: str) -> None:
    """拒绝 croniter 额外支持的 L、W、#、? 等非本项目五段扩展。"""
    fields = expression.upper().split()
    numeric = re.compile(r"[0-9*/,-]+")
    named = re.compile(r"[0-9A-Z*/,-]+")
    for index, field in enumerate(fields):
        matcher = named if index in {3, 4} else numeric
        if matcher.fullmatch(field) is None:
            raise ValueError(f"Cron 第 {index + 1} 段包含不支持的语法：{field}")
    allowed_names = {
        3: {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"},
        4: {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"},
    }
    for index, choices in allowed_names.items():
        for name in re.findall(r"[A-Z]+", fields[index]):
            if name not in choices:
                raise ValueError(f"Cron 第 {index + 1} 段包含未知名称：{name}")
