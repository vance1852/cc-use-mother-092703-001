"""确定性的 JSON 规范化与内容摘要工具。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Iterable


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"不能序列化 {type(value).__name__}")


def canonical_json(value: object) -> str:
    """生成跨平台一致、可重复的紧凑 JSON 文本。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容摘要。"""
    hasher = hashlib.sha256()
    for value in values:
        hasher.update(canonical_json(value).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def quantity_text(value: Any, field: str = "数量") -> str:
    """把数量统一成规范的十进制文本，拒绝非有限数和零负值。"""
    try:
        number = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{field}必须是十进制数值") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{field}必须大于零")
    return format(number, "f")
