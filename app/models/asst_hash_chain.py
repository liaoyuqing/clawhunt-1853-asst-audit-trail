"""哈希链：每日一条链（按 org_id + chain_date 分链），链尾写审计日志。"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class AsstHashChainEntry(UUIDPrimaryKeyMixin, Base):
    """链上一条记录：seq 为链内序号（从 1 起），prev_hash 为链上前一条的 row_hash。"""

    __tablename__ = "asst_hash_chain"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    chain_date: Mapped[date] = mapped_column(Date, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    trail_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trail_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 规范化 payload 原文（只含脱敏文本与指纹，不含明文），链因此可独立复核、不依赖业务行是否还在
    payload_canonical: Mapped[str] = mapped_column(Text, nullable=False)
    # 与 trail.occurred_at 同值：保留期清理按它判定能否删链记录
    occurred_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "org_id", "chain_date", "seq", name="uq_asst_hash_chain_org_date_seq"
        ),
        UniqueConstraint(
            "org_id", "chain_date", "trail_id", name="uq_asst_hash_chain_org_date_trail"
        ),
        Index("ix_asst_hash_chain_date_org", "chain_date", "org_id"),
    )


class AsstHashChainRun(UUIDPrimaryKeyMixin, Base):
    """每日链任务/校验的执行记录。同一天只允许一条（重跑幂等，不会产生第二条链）。"""

    __tablename__ = "asst_hash_chain_run"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    chain_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chain_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chain_tail_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_break_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    break_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "chain_date", name="uq_asst_hash_chain_run_org_date"),
    )