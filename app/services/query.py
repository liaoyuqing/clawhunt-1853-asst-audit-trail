"""审计查询：筛选条件、分页、数据范围收窄。

筛选口径（需求 ④ 与提案 ⑤ 一致）：
  user_id / from / to / tool_name / negative_feedback / blocked / conversation_id / kind / hidden_by_user
  * negative_feedback=true → 存在 rating <= 2 的反馈留痕的会话
  * blocked=true → 存在关停开关拦截事件的会话
  * tool_name → 会话里调用过该工具的会话
  * 列表按会话聚合（conversation 维度），详情按留痕行返回
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.asst_conversation import AsstConversation
from app.models.asst_trail import (
    TRAIL_KIND_BLOCKED,
    TRAIL_KIND_FEEDBACK,
    TRAIL_KIND_TOOL_CALL,
    AsstTrail,
)

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 20
NEGATIVE_RATING_MAX = 2


@dataclass(frozen=True)
class TrailFilter:
    """审计查询筛选条件；所有字段可选，空值表示不限制。"""

    user_id: uuid.UUID | None = None
    from_ts: datetime | None = None
    to_ts: datetime | None = None
    tool_name: str | None = None
    negative_feedback: bool | None = None
    blocked: bool | None = None
    conversation_id: uuid.UUID | None = None
    kind: str | None = None
    hidden_by_user: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        """序列化进导出任务的 filters 列（导出复现同一口径）。"""
        return {
            "user_id": str(self.user_id) if self.user_id else None,
            "from_ts": self.from_ts.isoformat() if self.from_ts else None,
            "to_ts": self.to_ts.isoformat() if self.to_ts else None,
            "tool_name": self.tool_name,
            "negative_feedback": self.negative_feedback,
            "blocked": self.blocked,
            "conversation_id": str(self.conversation_id) if self.conversation_id else None,
            "kind": self.kind,
            "hidden_by_user": self.hidden_by_user,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TrailFilter":
        data = data or {}

        def _uuid(key: str) -> uuid.UUID | None:
            value = data.get(key)
            return uuid.UUID(value) if value else None

        def _dt(key: str) -> datetime | None:
            value = data.get(key)
            return datetime.fromisoformat(value) if value else None

        return cls(
            user_id=_uuid("user_id"),
            from_ts=_dt("from_ts"),
            to_ts=_dt("to_ts"),
            tool_name=data.get("tool_name"),
            negative_feedback=data.get("negative_feedback"),
            blocked=data.get("blocked"),
            conversation_id=_uuid("conversation_id"),
            kind=data.get("kind"),
            hidden_by_user=data.get("hidden_by_user"),
        )


def _conversation_predicates(filter_: TrailFilter) -> list:
    """把筛选条件翻译成「会话维度」谓词。"""
    predicates: list = []
    if filter_.user_id is not None:
        predicates.append(AsstConversation.user_id == filter_.user_id)
    if filter_.conversation_id is not None:
        predicates.append(AsstConversation.id == filter_.conversation_id)
    if filter_.hidden_by_user is not None:
        predicates.append(AsstConversation.hidden_by_user == filter_.hidden_by_user)
    return predicates


def _trail_subquery_predicates(filter_: TrailFilter) -> list:
    """需要看留痕内容才能判定的条件（工具名、差评、拦截、时间窗）。"""
    predicates: list = []
    if filter_.tool_name is not None:
        predicates.append(
            exists().where(
                AsstTrail.conversation_id == AsstConversation.id,
                AsstTrail.kind == TRAIL_KIND_TOOL_CALL,
                AsstTrail.tool_name == filter_.tool_name,
            )
        )
    if filter_.negative_feedback is True:
        predicates.append(
            exists().where(
                AsstTrail.conversation_id == AsstConversation.id,
                AsstTrail.kind == TRAIL_KIND_FEEDBACK,
                AsstTrail.rating <= NEGATIVE_RATING_MAX,
            )
        )
    elif filter_.negative_feedback is False:
        predicates.append(
            ~exists().where(
                AsstTrail.conversation_id == AsstConversation.id,
                AsstTrail.kind == TRAIL_KIND_FEEDBACK,
                AsstTrail.rating <= NEGATIVE_RATING_MAX,
            )
        )
    if filter_.blocked is True:
        predicates.append(
            exists().where(
                AsstTrail.conversation_id == AsstConversation.id,
                AsstTrail.kind == TRAIL_KIND_BLOCKED,
            )
        )
    elif filter_.blocked is False:
        predicates.append(
            ~exists().where(
                AsstTrail.conversation_id == AsstConversation.id,
                AsstTrail.kind == TRAIL_KIND_BLOCKED,
            )
        )
    if filter_.from_ts is not None or filter_.to_ts is not None:
        window = [AsstTrail.conversation_id == AsstConversation.id]
        if filter_.from_ts is not None:
            window.append(AsstTrail.occurred_at >= filter_.from_ts)
        if filter_.to_ts is not None:
            window.append(AsstTrail.occurred_at <= filter_.to_ts)
        predicates.append(exists().where(*window))
    return predicates


def build_conversation_query(
    *,
    org_id: uuid.UUID,
    filters: TrailFilter,
    site_ids: Sequence[uuid.UUID] = (),
) -> Select:
    """会话列表查询：按 org 收窄，再按 S2-IAM 的 site 数据范围收窄。"""
    stmt = select(AsstConversation).where(AsstConversation.org_id == org_id)
    if site_ids:
        stmt = stmt.where(AsstConversation.site_id.in_(list(site_ids)))
    for predicate in _conversation_predicates(filters):
        stmt = stmt.where(predicate)
    for predicate in _trail_subquery_predicates(filters):
        stmt = stmt.where(predicate)
    return stmt


def query_trails(
    session: Session,
    *,
    org_id: uuid.UUID,
    filters: TrailFilter,
    site_ids: Sequence[uuid.UUID] = (),
    limit: int | None = None,
    offset: int = 0,
) -> list[AsstTrail]:
    """导出与详情共用的留痕行查询（按 seq 升序）。"""
    conv_stmt = build_conversation_query(org_id=org_id, filters=filters, site_ids=site_ids)
    conv_ids = session.execute(conv_stmt.with_only_columns(AsstConversation.id)).scalars().all()
    if not conv_ids:
        return []
    stmt = select(AsstTrail).where(
        AsstTrail.org_id == org_id, AsstTrail.conversation_id.in_(list(conv_ids))
    )
    if filters.kind is not None:
        stmt = stmt.where(AsstTrail.kind == filters.kind)
    if filters.from_ts is not None:
        stmt = stmt.where(AsstTrail.occurred_at >= filters.from_ts)
    if filters.to_ts is not None:
        stmt = stmt.where(AsstTrail.occurred_at <= filters.to_ts)
    stmt = stmt.order_by(AsstTrail.seq).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars())


def list_conversations(
    session: Session,
    *,
    org_id: uuid.UUID,
    filters: TrailFilter,
    site_ids: Sequence[uuid.UUID] = (),
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[dict[str, Any]], int]:
    """返回 (会话行列表, 总数)。分页上限 MAX_PAGE_SIZE，防止一次拉爆。"""
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    base = build_conversation_query(org_id=org_id, filters=filters, site_ids=site_ids)
    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    rows = session.execute(
        base.order_by(AsstConversation.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).scalars().all()
    return [summarize_conversation(session, conversation) for conversation in rows], total


def summarize_conversation(session: Session, conversation: AsstConversation) -> dict[str, Any]:
    """会话行摘要：条数、最近时间、是否有差评/拦截、工具名集合。"""
    stats = session.execute(
        select(
            func.count(AsstTrail.id),
            func.min(AsstTrail.occurred_at),
            func.max(AsstTrail.occurred_at),
        ).where(AsstTrail.conversation_id == conversation.id)
    ).one()
    tool_names = session.execute(
        select(AsstTrail.tool_name)
        .where(
            AsstTrail.conversation_id == conversation.id,
            AsstTrail.tool_name.is_not(None),
        )
        .distinct()
    ).scalars().all()
    negative = session.execute(
        select(func.count(AsstTrail.id)).where(
            AsstTrail.conversation_id == conversation.id,
            AsstTrail.kind == TRAIL_KIND_FEEDBACK,
            AsstTrail.rating <= NEGATIVE_RATING_MAX,
        )
    ).scalar_one()
    blocked = session.execute(
        select(func.count(AsstTrail.id)).where(
            AsstTrail.conversation_id == conversation.id,
            AsstTrail.kind == TRAIL_KIND_BLOCKED,
        )
    ).scalar_one()
    return {
        "id": str(conversation.id),
        "org_id": str(conversation.org_id),
        "site_id": str(conversation.site_id) if conversation.site_id else None,
        "user_id": str(conversation.user_id),
        "title": conversation.title,
        "hidden_by_user": conversation.hidden_by_user,
        "deleted_at": conversation.deleted_at.isoformat() if conversation.deleted_at else None,
        "created_at": conversation.created_at.isoformat(),
        "trail_count": int(stats[0]),
        "first_occurred_at": stats[1].isoformat() if stats[1] else None,
        "last_occurred_at": stats[2].isoformat() if stats[2] else None,
        "tool_names": sorted({t for t in tool_names if t}),
        "negative_feedback_count": int(negative),
        "blocked_event_count": int(blocked),
    }