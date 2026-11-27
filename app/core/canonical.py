"""确定性 JSON 序列化：哈希链的输入必须是逐字节可复现的。

规则（写死在代码里，测试断言）：
  * 对象键名按字典序排序；
  * 分隔符无空格（`{"a":1}`）；
  * 时间统一转成 UTC 秒级 ISO（`2026-01-02T03:04:05Z`），不带微秒、不随时区漂移；
  * UUID 转小写字符串；浮点转最短往返字符串；
  * `ensure_ascii=False` 保证中文按 UTF-8 编码，而不是 Unicode 转义。
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any


def normalize(value: Any) -> Any:
    """把任意值递归转换成可确定性序列化的基础类型。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            # NaN/Inf 不进入 JSON，统一按字符串保留可读性，避免不同实现写出不同字面量
            return str(value)
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, uuid.UUID):
        return str(value).lower()
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [normalize(v) for v in value]
        if isinstance(value, (set, frozenset)):
            # 集合本身无序，按其规范化后的 JSON 字面量排序，保证跨进程一致
            items.sort(key=lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True))
        return items
    return str(value)


def canonical_json(value: Any) -> str:
    """返回确定性 JSON 字符串（同一个值在任何进程、任何机器上字节一致）。"""
    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    """UTF-8 编码的规范化字节，直接喂给 hashlib。"""
    return canonical_json(value).encode("utf-8")