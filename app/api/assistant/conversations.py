"""业务侧最小接口：新建会话、写 5 类留痕、删除会话（只隐藏）。

这几条不是本单的交付重点（本单交付的是留痕与审计），但没有它们无法演示
「用户在界面删除会话后，审计接口仍能查到完整内容」。AI 助手本体属其他工单，
这里只做留痕落点。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_cipher, get_principal, get_session
from app.services.crypto import TrailCipher
from app.services.conversations import (
    ConversationNotFound,
    create_conversation,
    hide_conversation,
)
from app.services.iam import Principal
from app.services.trail_writer import TrailWriter

router = APIRouter(prefix="/api/assistant/conversations", tags=["assistant-conversations"])


class ConversationCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    site_id: uuid.UUID | None = None


class ConversationCreated(BaseModel):
    id: str
    user_id: str
    title: str


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    occurred_at: datetime | None = None


class AnswerIn(BaseModel):
    content: str = Field(min_length=1, max_length=50000)
    doc_refs: list[dict[str, Any]] = Field(default_factory=list)
    model_name: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=64)
    occurred_at: datetime | None = None


class ToolCallIn(BaseModel):
    tool_name: str = Field(min_length=1, max_length=128)
    tool_version: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    result_summary: str = Field(default="", max_length=20000)
    status: str = Field(default="ok", max_length=32)
    confirmed_by: uuid.UUID | None = None
    confirmed_at: datetime | None = None
    occurred_at: datetime | None = None


class BlockedEventIn(BaseModel):
    block_reason: str = Field(min_length=1, max_length=64)
    rule_id: str = Field(min_length=1, max_length=64)
    attempted_tool: str | None = Field(default=None, max_length=128)
    detail: str | None = Field(default=None, max_length=2000)
    occurred_at: datetime | None = None


class FeedbackIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)
    related_trail_id: uuid.UUID | None = None
    occurred_at: datetime | None = None


class TrailCreated(BaseModel):
    id: str
    seq: int
    kind: str
    content_sha256: str | None = None


def _conversation_or_404(session: Session, principal: Principal, conversation_id: uuid.UUID):
    from app.services.conversations import get_conversation

    conversation = get_conversation(
        session, org_id=principal.org_id, conversation_id=conversation_id
    )
    if conversation is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "CONVERSATION_NOT_FOUND", "message": "会话不存在"},
        )
    return conversation


@router.post("", response_model=ConversationCreated, status_code=status.HTTP_201_CREATED)
def create(
    payload: ConversationCreate,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[Session, Depends(get_session)],
) -> ConversationCreated:
    conversation = create_conversation(
        session,
        org_id=principal.org_id,
        user_id=principal.operator_id,
        site_id=payload.site_id or (principal.site_ids[0] if principal.site_ids else None),
        title=payload.title,
    )
    session.commit()
    return ConversationCreated(
        id=str(conversation.id),
        user_id=str(conversation.user_id),
        title=conversation.title,
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    """界面「删除会话」：只置 hidden_by_user，返回 204；留痕一行都不删。"""
    try:
        hide_conversation(
            session,
            org_id=principal.org_id,
            conversation_id=conversation_id,
            user_id=principal.operator_id,
        )
    except ConversationNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "CONVERSATION_NOT_FOUND", "message": str(exc)},
        ) from exc
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{conversation_id}/messages",
    response_model=TrailCreated,
    status_code=status.HTTP_201_CREATED,
)
def post_message(
    conversation_id: uuid.UUID,
    payload: MessageIn,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[Session, Depends(get_session)],
    cipher: Annotated[TrailCipher, Depends(get_cipher)],
) -> TrailCreated:
    conversation = _conversation_or_404(session, principal, conversation_id)
    trail = TrailWriter(session, cipher).record_message(
        conversation=conversation,
        user_id=principal.operator_id,
        content=payload.content,
        occurred_at=payload.occurred_at,
    )
    session.commit()
    return TrailCreated(
        id=str(trail.id), seq=trail.seq, kind=trail.kind, content_sha256=trail.content_sha256
    )


@router.post(
    "/{conversation_id}/answers",
    response_model=TrailCreated,
    status_code=status.HTTP_201_CREATED,
)
def post_answer(
    conversation_id: uuid.UUID,
    payload: AnswerIn,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[Session, Depends(get_session)],
    cipher: Annotated[TrailCipher, Depends(get_cipher)],
) -> TrailCreated:
    conversation = _conversation_or_404(session, principal, conversation_id)
    trail = TrailWriter(session, cipher).record_answer(
        conversation=conversation,
        user_id=principal.operator_id,
        content=payload.content,
        doc_refs=payload.doc_refs,
        model_name=payload.model_name,
        prompt_version=payload.prompt_version,
        occurred_at=payload.occurred_at,
    )
    session.commit()
    return TrailCreated(
        id=str(trail.id), seq=trail.seq, kind=trail.kind, content_sha256=trail.content_sha256
    )


@router.post(
    "/{conversation_id}/tool-calls",
    response_model=TrailCreated,
    status_code=status.HTTP_201_CREATED,
)
def post_tool_call(
    conversation_id: uuid.UUID,
    payload: ToolCallIn,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[Session, Depends(get_session)],
    cipher: Annotated[TrailCipher, Depends(get_cipher)],
) -> TrailCreated:
    conversation = _conversation_or_404(session, principal, conversation_id)
    trail = TrailWriter(session, cipher).record_tool_call(
        conversation=conversation,
        user_id=principal.operator_id,
        tool_name=payload.tool_name,
        tool_version=payload.tool_version,
        params=payload.params,
        result_summary=payload.result_summary,
        status=payload.status,
        confirmed_by=payload.confirmed_by,
        confirmed_at=payload.confirmed_at,
        occurred_at=payload.occurred_at,
    )
    session.commit()
    return TrailCreated(
        id=str(trail.id), seq=trail.seq, kind=trail.kind, content_sha256=trail.content_sha256
    )


@router.post(
    "/{conversation_id}/blocked-events",
    response_model=TrailCreated,
    status_code=status.HTTP_201_CREATED,
)
def post_blocked_event(
    conversation_id: uuid.UUID,
    payload: BlockedEventIn,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[Session, Depends(get_session)],
    cipher: Annotated[TrailCipher, Depends(get_cipher)],
) -> TrailCreated:
    conversation = _conversation_or_404(session, principal, conversation_id)
    trail = TrailWriter(session, cipher).record_blocked_event(
        conversation=conversation,
        user_id=principal.operator_id,
        block_reason=payload.block_reason,
        rule_id=payload.rule_id,
        attempted_tool=payload.attempted_tool,
        detail=payload.detail,
        occurred_at=payload.occurred_at,
    )
    session.commit()
    return TrailCreated(
        id=str(trail.id), seq=trail.seq, kind=trail.kind, content_sha256=trail.content_sha256
    )


@router.post(
    "/{conversation_id}/feedback",
    response_model=TrailCreated,
    status_code=status.HTTP_201_CREATED,
)
def post_feedback(
    conversation_id: uuid.UUID,
    payload: FeedbackIn,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[Session, Depends(get_session)],
    cipher: Annotated[TrailCipher, Depends(get_cipher)],
) -> TrailCreated:
    conversation = _conversation_or_404(session, principal, conversation_id)
    trail = TrailWriter(session, cipher).record_feedback(
        conversation=conversation,
        user_id=principal.operator_id,
        rating=payload.rating,
        comment=payload.comment,
        related_trail_id=payload.related_trail_id,
        occurred_at=payload.occurred_at,
    )
    session.commit()
    return TrailCreated(
        id=str(trail.id), seq=trail.seq, kind=trail.kind, content_sha256=trail.content_sha256
    )
