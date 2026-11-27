"""引擎与会话工厂。生产 PostgreSQL 15+，测试/离线用 SQLite（行为差异见 README）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, load_settings
from app.db.base import Base
from app.db.immutability import install_immutability


def build_engine(settings: Settings | None = None) -> Engine:
    """按 ASST_DATABASE_URL 建引擎；SQLite 需先把目录建出来。"""
    settings = settings or load_settings()
    url = settings.sqlalchemy_url
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        db_path = url.split("///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_size"] = int(os.environ.get("ASST_DB_POOL_SIZE", "10"))
        kwargs["max_overflow"] = 5
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        _enable_sqlite_pragmas(engine)
    return engine


def _enable_sqlite_pragmas(engine: Engine) -> None:
    """SQLite 默认外键关闭、日志模式回滚；显式打开以贴近 PG 行为。"""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.close()


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_storage(engine: Engine) -> None:
    """建表 + 安装不可变触发器。幂等，可重复调用。

    生产环境走 Alembic（见 migrations/），测试与本地开发走这里：
    两者建的触发器是同一套 DDL，Alembic 迁移里也有对应语句。
    """
    Base.metadata.create_all(engine)
    install_immutability(engine)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
