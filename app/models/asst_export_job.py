"""异步导出任务与下载令牌。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BusinessColumnsMixin, UUIDPrimaryKeyMixin, utcnow


class AsstExportJob(UUIDPrimaryKeyMixin, BusinessColumnsMixin, Base):
    __tablename__ = "asst_export_job"

    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    filters: Mapped[dict] = mapped_column(JSON, nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False, default="jsonl")
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    __table_args__ = (
        Index("ix_asst_export_job_org_created", "org_id", "created_at"),
    )


class AsstExportToken(UUIDPrimaryKeyMixin, Base):
    """下载令牌：只存 sha256，10 分钟有效、单次可用。"""

    __tablename__ = "asst_export_token"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
