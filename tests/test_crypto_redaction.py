"""加密、脱敏、规范化 JSON 的单元测试（留痕格式的底层不变量）。"""
from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest

from app.core.canonical import canonical_json, normalize
from app.services.crypto import (
    ENVELOPE_PREFIX,
    DecryptionError,
    TrailCipher,
    TrailKeyMissing,
    derive_key,
    sha256_hex,
)
from app.services.crypto import redact_text, redact_value


def test_canonical_json_is_deterministic():
    """键序、浮点、时间、UUID 全部确定性：同一值两次序列化字节一致。"""
    payload = {
        "b": 2,
        "a": [3, 1, 2],
        "when": dt.datetime(2026, 1, 2, 3, 4, 5, 678901),
        "ratio": 0.1,
        "id": uuid.UUID("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        "中文": "值",
    }
    first = canonical_json(payload)
    assert first == canonical_json(dict(reversed(list(payload.items()))))
    assert '"when":"2026-01-02T03:04:05Z"' in first
    assert '"id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"' in first
    assert "中文" in first  # ensure_ascii=False：中文按 UTF-8 落，不转义
    assert " " not in first  # 无多余空格


def test_canonical_json_handles_naive_and_aware():
    naive = dt.datetime(2026, 5, 6, 7, 8, 9)
    aware = dt.datetime(2026, 5, 6, 7, 8, 9, tzinfo=dt.timezone.utc)
    assert canonical_json({"t": naive}) == canonical_json({"t": aware})


def test_canonical_json_stable_across_processes():
    """同一 payload 在两次调用中哈希一致（不依赖 dict 插入顺序）。"""
    one = {"z": 1, "a": {"y": 2, "b": 3}}
    two = {"a": {"b": 3, "y": 2}, "z": 1}
    assert sha256_hex(canonical_json(one).encode()) == sha256_hex(canonical_json(two).encode())


def test_encrypt_decrypt_roundtrip():
    cipher = TrailCipher.from_env_value("unit-test-key-material-32-bytes-ok")
    envelope = cipher.encrypt("会员 13812345678 的对接记录", aad=b"aad-1")
    assert envelope.startswith(ENVELOPE_PREFIX)
    assert "13812345678" not in envelope
    assert cipher.decrypt(envelope, aad=b"aad-1") == "会员 13812345678 的对接记录"


def test_ciphertext_is_randomised():
    """同一明文两次加密密文不同（nonce 随机），但都能解回原文。"""
    cipher = TrailCipher.from_env_value("unit-test-key-material-32-bytes-ok")
    first = cipher.encrypt("same")
    second = cipher.encrypt("same")
    assert first != second
    assert cipher.decrypt(first) == cipher.decrypt(second) == "same"


def test_wrong_aad_rejected():
    """AAD 不匹配（密文被搬到别的行）→ GCM 校验失败。"""
    cipher = TrailCipher.from_env_value("unit-test-key-material-32-bytes-ok")
    envelope = cipher.encrypt("secret", aad=b"row-1")
    with pytest.raises(DecryptionError):
        cipher.decrypt(envelope, aad=b"row-2")


def test_wrong_key_rejected():
    first = TrailCipher.from_env_value("key-material-number-one-32bytes!!!")
    second = TrailCipher.from_env_value("key-material-number-two-32bytes!!!")
    envelope = first.encrypt("secret")
    with pytest.raises(DecryptionError):
        second.decrypt(envelope)


def test_missing_key_refused():
    """没有 ASST_TRAIL_KEY 时拒绝加密（fail-closed，不退化成明文）。"""
    with pytest.raises(TrailKeyMissing):
        TrailCipher.from_env_value(None)


def test_key_formats():
    """32 字节 base64 / 64 位 hex / 任意口令三种写法都能派生出同一个 32 字节密钥口径。"""
    import base64

    raw32 = bytes(range(32))
    assert derive_key(base64.b64encode(raw32).decode()) == raw32
    assert derive_key(raw32.hex()) == raw32
    passphrase_key = derive_key("a long passphrase that is not a key")
    assert len(passphrase_key) == 32
    assert passphrase_key != raw32


def test_redaction_rules():
    """手机号中 4 位、身份证全掩、邮箱本地部分留 2 位。"""
    text = "联系 13812345678，身份证 110101199003071234，邮箱 zhangsan@example.com"
    redacted = redact_text(text)
    assert "138****5678" in redacted
    assert "13812345678" not in redacted
    assert "110101199003071234" not in redacted
    assert "11****************" in redacted
    assert "zh***@example.com" in redacted
    assert "zhangsan" not in redacted


def test_redaction_of_structures():
    value = {
        "phone": "13812345678",
        "nested": {"email": "lisi@example.com", "count": 3},
        "items": ["13900001111"],
        "password": "plaintext-secret",
    }
    redacted = redact_value(value)
    assert redacted["phone"] == "138****5678"
    assert redacted["nested"]["email"] == "li***@example.com"
    assert redacted["nested"]["count"] == 3
    assert redacted["items"][0] == "139****1111"
    assert "plaintext-secret" not in json.dumps(redacted, ensure_ascii=False)


def test_redaction_keeps_none():
    assert redact_text(None) is None
    assert redact_value({"a": None})["a"] is None


def test_normalize_datetime_and_sets():
    normalized = normalize({
        "t": dt.datetime(2026, 1, 2, 3, 4, 5, 999999),
        "d": dt.date(2026, 1, 2),
        "s": {"b", "a"},
        "u": uuid.UUID("11111111-1111-4111-8111-111111111111"),
    })
    assert normalized["t"] == "2026-01-02T03:04:05Z"
    assert normalized["d"] == "2026-01-02"
    assert normalized["s"] == ["a", "b"]
    assert normalized["u"] == "11111111-1111-4111-8111-111111111111"
