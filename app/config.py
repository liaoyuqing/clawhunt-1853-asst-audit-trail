"""配置与密钥只从环境变量读取；代码里不出现任何密钥默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_RETENTION_DAYS = 180
DEFAULT_EXPORT_TTL_SECONDS = 600
DEFAULT_PURGE_BATCH_SIZE = 1000
DEFAULT_EXPORT_MAX_ROWS = 100000
DEFAULT_REDIS_KEY_PREFIX = "asst1853:"
DEFAULT_REDIS_BRPOP_TIMEOUT = 5


class ConfigError(RuntimeError):
    """环境变量缺失或取值非法。"""


def _raw(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int(name: str, default: int) -> int:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前值 {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    database_url: str
    trail_key: str | None
    export_signing_key: str | None
    retention_days: int
    purge_batch_size: int
    export_dir: str
    public_base_url: str
    export_url_ttl_seconds: int
    export_max_rows: int
    export_queue: str
    redis_url: str | None = None
    redis_key_prefix: str = DEFAULT_REDIS_KEY_PREFIX
    redis_brpop_timeout: int = DEFAULT_REDIS_BRPOP_TIMEOUT

    @property
    def sqlalchemy_url(self) -> str:
        """把 postgres:// / postgresql:// 归一成 psycopg3 驱动。"""
        url = self.database_url
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url[len("postgres://") :]
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://") :]
        return url


def load_settings() -> Settings:
    """每次调用都重新读环境变量，便于测试用例按用例改配置。"""
    database_url = _raw("ASST_DATABASE_URL") or "sqlite+pysqlite:///./var/asst.db"
    public_base_url = (_raw("ASST_PUBLIC_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    export_queue = (_raw("ASST_EXPORT_QUEUE") or "thread").lower()
    if export_queue not in {"inline", "thread", "redis"}:
        raise ConfigError("ASST_EXPORT_QUEUE 只支持 inline / thread / redis")
    redis_url = _raw("ASST_REDIS_URL")
    if export_queue == "redis" and not redis_url:
        raise ConfigError("ASST_EXPORT_QUEUE=redis 时必须给出 ASST_REDIS_URL")
    return Settings(
        database_url=database_url,
        trail_key=_raw("ASST_TRAIL_KEY"),
        export_signing_key=_raw("ASST_EXPORT_SIGNING_KEY"),
        retention_days=_int("ASST_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
        purge_batch_size=_int("ASST_PURGE_BATCH_SIZE", DEFAULT_PURGE_BATCH_SIZE),
        export_dir=_raw("ASST_EXPORT_DIR") or "./var/exports",
        public_base_url=public_base_url,
        export_url_ttl_seconds=_int("ASST_EXPORT_URL_TTL_SECONDS", DEFAULT_EXPORT_TTL_SECONDS),
        export_max_rows=_int("ASST_EXPORT_MAX_ROWS", DEFAULT_EXPORT_MAX_ROWS),
        export_queue=export_queue,
        redis_url=redis_url,
        redis_key_prefix=_raw("ASST_REDIS_KEY_PREFIX") or DEFAULT_REDIS_KEY_PREFIX,
        redis_brpop_timeout=_int("ASST_REDIS_BRPOP_TIMEOUT", DEFAULT_REDIS_BRPOP_TIMEOUT),
    )
