"""会话侧写入路径：新建会话、删除会话（只隐藏）、以及对外读取。

「删除会话」在这里被实现成 hidden_by_user=True —— 本模块**没有**任何删除留痕行的代码，
也调不到：物理删除的唯一入口是 app/services/retention.py 的清理任务。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.asst_conversation import AsstConversation
from app.models.asst_trail import AsstTrail


class ConversationNotFound(LookupError):
    pass


def create_conversation(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    site_id: uuid.UUID | None = None,
    title: str = "",
) -> AsstConversation:
    conversation = AsstConversation(
        org_id=org_id,
        site_id=site_id,
        user_id=user_id,
        title=title,
        created_by=user_id,
    )
    session.add(conversation)
    session.flush()
    return conversation


def hide_conversation(
    session: Session, *, org_id: uuid.UUID, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> AsstConversation:
    """界面「删除会话」= 置 hidden_by_user；留痕一行不少，审计接口照常可查。

    hidden_by_user 是留痕表白名单里的两列之一，所以这里能更新成功；
    其余列任何改动都会被触发器拒绝。
    """
    conversation = session.get(AsstConversation, conversation_id)
    if conversation is None or conversation.org_id != org_id or conversation.user_id != user_id:
        raise ConversationNotFound("会话不存在或不属于当前用户")
    conversation.hidden_by_user = True
    conversation.updated_at = utcnow()
    conversation.deleted_at = utcnow()
    # 留痕行同步打上隐藏标记，导出/列表的「已隐藏」口径与之一致（同样是白名单列）
    session.execute(
        update(AsstTrail)
        .where(
            AsstTrail.conversation_id == conversation_id,
            AsstTrail.org_id == org_id,
        )
        .values(hidden_by_user=True)
    )
    session.flush()
    return conversation


def get_conversation(
    session: Session, *, org_id: uuid.UUID, conversation_id: uuid.UUID
) -> AsstConversation | None:
    conversation = session.get(AsstConversation, conversation_id)
    if conversation is None or conversation.org_id != org_id:
        return None
    return conversation


def list_trails(
    session: Session, *, org_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[AsstTrail]:
    stmt = (
        select(AsstTrail)
        .where(AsstTrail.org_id == org_id, AsstTrail.conversation_id == conversation_id)
        .order_by(AsstTrail.seq)
    )
    return list(session.execute(stmt).scalars())
