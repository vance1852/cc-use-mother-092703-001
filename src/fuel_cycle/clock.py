"""可注入时间源与 UTC 文本解析。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        if self.current.tzinfo is None:
            raise ValueError("冻结时钟必须带时区")
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: object, field: str = "时间") -> str:
    """解析 ISO-8601 时间并归一化为 UTC 文本，拒绝朴素时间。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是 ISO-8601 时间字符串")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法的 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须显式携带时区")
    return isoformat(parsed)
