"""留痕主表：只增不改（DB 触发器兜底），仅两列可更新。"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    JSON,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BusinessColumnsMixin, UUIDPrimaryKeyMixin, utcnow

TRAIL_KIND_MESSAGE = "message"
TRAIL_KIND_ANSWER = "answer"
TRAIL_KIND_TOOL_CALL = "tool_call"
TRAIL_KIND_BLOCKED = "blocked_event"
TRAIL_KIND_FEEDBACK = "feedback"
TRAIL_KINDS = (
    TRAIL_KIND_MESSAGE,
    TRAIL_KIND_ANSWER,
    TRAIL_KIND_TOOL_CALL,
    TRAIL_KIND_BLOCKED,
    TRAIL_KIND_FEEDBACK,
)


class AsstTrail(UUIDPrimaryKeyMixin, BusinessColumnsMixin, Base):
    """一条留痕记录。除 hidden_by_user / purged_at 外，任何列被改都会触发 DB 异常。"""

    __tablename__ = "asst_trail"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chain_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=utcnow, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    related_trail_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    # 消息 / 回答正文：原文加密列 + 脱敏列；正文原文永不落明文
    content_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 回答：引用文档与版本、模型名、提示词版本
    doc_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 工具调用：工具名、版本、脱敏参数、结果摘要、确认人与确认时间
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tool_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params_redacted: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 关停开关拦截事件
    block_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 用户反馈
    rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    comment_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 参与哈希链的规范化快照（写入时冻结，永不更新）
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    # 仅这两列允许被 UPDATE（列级白名单）
    hidden_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 留痕是只增不减的行：updated_at 恒等于 created_at，这里刻意覆盖掉基类的 onupdate，
    # 否则任何 ORM UPDATE 都会顺带写 updated_at，白名单以外的列一出现就被触发器拒绝
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("org_id", "seq", name="uq_asst_trail_org_seq"),
        Index("ix_asst_trail_org_chain_date_seq", "org_id", "chain_date", "seq"),
        Index("ix_asst_trail_conv_occurred", "conversation_id", "occurred_at"),
        Index("ix_asst_trail_org_user_occurred", "org_id", "user_id", "occurred_at"),
    )


class AsstTrailCounter(UUIDPrimaryKeyMixin, Base):
    """按 org 递增的留痕序号计数器：保证 seq 单调且可跨天连续。"""

    __tablename__ = "asst_trail_counter"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, default="trail", server_default="trail")
    last_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )


class AsstPurgeMarker(UUIDPrimaryKeyMixin, Base):
    """保留期清理写下的「已删除段」标记，供哈希链校验把删除段与篡改区分开。"""

    __tablename__ = "asst_purge_marker"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    chain_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # 被删段的留痕序号区间（全局 seq）
    purged_from_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purged_to_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 被删段对应链记录的链内序号区间（链内序号从 1 起）
    chain_seq_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chain_seq_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purged_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    # 被删掉的最后一条链记录的 row_hash：校验时用它把删除段与篡改区分开
    last_purged_row_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audit_log_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    __table_args__ = (
        Index("ix_asst_purge_marker_org_range", "org_id", "purged_from_seq", "purged_to_seq"),
        Index("ix_asst_purge_marker_org_date", "org_id", "chain_date"),
    )