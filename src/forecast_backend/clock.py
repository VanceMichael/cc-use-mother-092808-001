"""时钟抽象：业务时间统一来自 Clock，便于测试与重启恢复。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """返回当前时刻（带时区）。"""

    def now(self) -> datetime: ...


class SystemClock:
    """生产环境使用的真实时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """测试用手动时钟，可精确控制时间推进。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._current = start

    def now(self) -> datetime:
        return self._current

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        self._current = value

    def advance(self, **kwargs: float) -> None:
        self._current = self._current + timedelta(**kwargs)


def iso(value: datetime) -> str:
    """统一的时间序列化格式；同格式 ISO 字符串可按字典序比较。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
