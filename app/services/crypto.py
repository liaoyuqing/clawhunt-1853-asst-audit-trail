"""留痕原文加密 + 脱敏（提案承诺的本单实现位置）。

加密：AES-256-GCM + HKDF-SHA256 密钥派生
  * 密钥只从环境变量 ``ASST_TRAIL_KEY`` 读（见 app/config.py），代码里没有任何默认密钥；
  * 接受三种写法：32 字节 base64 / 64 位 hex / 任意长度口令（口令走 HKDF 派生）；
  * 密文格式 ``enc:v1:<base64(nonce12 || ciphertext || tag16)>``，版本前缀为将来轮换预留；
  * AAD 绑定留痕行 id：整段密文被搬到别的行会解密失败。

脱敏：手机号中 4 位、身份证全掩、邮箱本地部分留 2 位、卡号保首尾；凭据类键整值掩掉。
脱敏文本与加密原文同时落库：列表与导出默认展示脱敏文本，详情接口才解密。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


ENVELOPE_PREFIX = "enc:v1:"
_HKDF_SALT = b"clawhunt/asst-trail/v1"
_HKDF_INFO = b"asst-trail-content-encryption"


class TrailKeyMissing(RuntimeError):
    """未配置 ASST_TRAIL_KEY：留痕写入必须失败关闭，不能悄悄退化成明文。"""


class DecryptionError(RuntimeError):
    """密文损坏、密钥不匹配或信封版本不支持。"""


def _decode_key_material(raw: str) -> bytes:
    candidate = raw.strip()
    if not candidate:
        raise TrailKeyMissing("ASST_TRAIL_KEY 为空")
    if len(candidate) == 64:
        try:
            return bytes.fromhex(candidate)
        except ValueError:
            pass
    try:
        decoded = base64.b64decode(candidate, validate=True)
        if len(decoded) == 32:
            return decoded
    except (binascii.Error, ValueError):
        pass
    # 口令：用 HKDF 派生 32 字节，避免把任意长度口令直接当密钥
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALT, info=_HKDF_INFO
    ).derive(candidate.encode("utf-8"))


def derive_key(raw: str) -> bytes:
    """把环境变量里的密钥材料统一派生成 32 字节 AES 密钥。"""
    return _decode_key_material(raw)


def derive_signing_key(trail_key_raw: str, explicit: str | None = None) -> bytes:
    """导出下载令牌的 HMAC 密钥：优先 ASST_EXPORT_SIGNING_KEY，否则从主密钥派生。"""
    if explicit:
        return hmac.new(
            _decode_key_material(explicit), b"asst-export-signing-v1", hashlib.sha256
        ).digest()
    return hmac.new(
        _decode_key_material(trail_key_raw), b"asst-export-signing-v1", hashlib.sha256
    ).digest()


class TrailCipher:
    """AES-256-GCM 信封加解密。实例不可序列化，密钥不与密文同处一地。"""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256 需要 32 字节密钥")
        self._aes = AESGCM(key)

    @classmethod
    def from_env_value(cls, raw: str | None) -> "TrailCipher":
        if raw is None:
            raise TrailKeyMissing(
                "缺少环境变量 ASST_TRAIL_KEY：留痕原文必须加密存储，拒绝以明文写入"
            )
        return cls(derive_key(raw))

    def encrypt(self, plaintext: str, aad: bytes | None = None) -> str:
        nonce = os.urandom(12)
        sealed = self._aes.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return ENVELOPE_PREFIX + base64.b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, envelope: str, aad: bytes | None = None) -> str:
        if not envelope.startswith(ENVELOPE_PREFIX):
            raise DecryptionError("不是 enc:v1 信封格式")
        try:
            blob = base64.b64decode(envelope[len(ENVELOPE_PREFIX) :], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DecryptionError("密文 base64 解码失败") from exc
        if len(blob) < 12 + 16:
            raise DecryptionError("密文长度不足")
        nonce, sealed = blob[:12], blob[12:]
        try:
            return self._aes.decrypt(nonce, sealed, aad).decode("utf-8")
        except InvalidTag as exc:
            raise DecryptionError("GCM 校验失败：密钥不匹配或密文被改") from exc


def sha256_hex(data: bytes) -> str:
    """十六进制 sha256，供内容指纹与链哈希共用。"""
    return hashlib.sha256(data).hexdigest()



# 中国大陆手机号：11 位、1 开头、第二位 3-9
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 18 位身份证（末位可为 X）
_ID_CARD = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
# 邮箱
_EMAIL = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# 银行卡/长数字串（16-19 位）兜底
_BANK_CARD = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")

REDACTED_MARK = "***"


def _mask_phone(match: re.Match[str]) -> str:
    value = match.group(1)
    return value[:3] + "****" + value[-4:]


def _mask_id_card(match: re.Match[str]) -> str:
    value = match.group(1)
    return value[:2] + "*" * (len(value) - 2)


def _mask_email(match: re.Match[str]) -> str:
    local, domain = match.group(1), match.group(2)
    keep = local[:2] if len(local) >= 2 else local[:1]
    return f"{keep}{REDACTED_MARK}@{domain}"


def _mask_bank_card(match: re.Match[str]) -> str:
    value = match.group(1)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def redact_text(text: str | None) -> str | None:
    """对自由文本按固定顺序套用脱敏规则；None 原样返回。"""
    if text is None:
        return None
    result = _ID_CARD.sub(_mask_id_card, text)
    result = _PHONE.sub(_mask_phone, result)
    result = _BANK_CARD.sub(_mask_bank_card, result)
    result = _EMAIL.sub(_mask_email, result)
    return result


def redact_value(value: Any) -> Any:
    """对 JSON 结构递归脱敏：字符串走脱敏规则，键名命中敏感词的整值掩掉。"""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_mapping_entry(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    return value


# 凭据类键：整值掩掉（掩码后仍可读会泄漏秘密）
_CREDENTIAL_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "session_id",
)

# 标识类键：仍走模式脱敏，保留可读性（如 138****5678），便于审计比对
_IDENTIFIER_KEY_HINTS = ("phone", "mobile", "email", "id_card", "idcard")


def _redact_mapping_entry(key: str, value: Any) -> Any:
    if value in (None, "", [], {}):
        return value
    lowered = str(key).lower()
    if any(hint in lowered for hint in _CREDENTIAL_KEY_HINTS):
        return REDACTED_MARK
    if any(hint in lowered for hint in _IDENTIFIER_KEY_HINTS) and not isinstance(
        value, (dict, list, tuple)
    ):
        return redact_text(str(value))
    return redact_value(value)