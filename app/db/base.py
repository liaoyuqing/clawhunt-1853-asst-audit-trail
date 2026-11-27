"""声明基类与业务表通用列（需求统一技术约定）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """统一使用「朴素 UTC」时间戳：库里不存时区，语义一律 UTC。

    PostgreSQL 侧落 timestamp without time zone，SQLite 侧同为无时区文本；
    两边读取出来都是 naive datetime，避免 naive/aware 混用导致比较报错。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """全部 ORM 模型的声明基类。"""


class UUIDPrimaryKeyMixin:
    """业务表统一主键：UUID（跨库一致，SQLite 侧存 CHAR(32)）。"""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class BusinessColumnsMixin:
    """需求要求业务表统一含 org_id / site_id / 审计四列。"""

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    site_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
