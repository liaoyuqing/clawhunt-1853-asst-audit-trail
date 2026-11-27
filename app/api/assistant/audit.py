"""3 个审计接口 + 导出下载与任务查询（每个查看/导出动作本身都写审计日志）。

  GET  /api/assistant/audit/conversations          列表（筛选 + 分页）
  GET  /api/assistant/audit/conversations/{id}     详情（解密后完整内容）
  POST /api/assistant/audit/exports               异步导出 JSONL（202）
  GET  /api/assistant/audit/exports/{job_id}      任务状态 + 10 分钟下载链接
  GET  /api/assistant/audit/exports/{job}/download 凭令牌下载（单次、10 分钟）
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    client_ip,
    get_cipher,
    get_session,
    get_settings,
    require_auditor,
)
from app.config import Settings
from app.services.crypto import DecryptionError, TrailCipher
from app.models.asst_export_job import AsstExportJob
from app.services.audit_log import (
    ACTION_AUDIT_EXPORT,
    ACTION_AUDIT_VIEW,
    ACTION_EXPORT_DOWNLOAD,
    AuditEvent,
    write_audit_log,
)
from app.services.conversations import get_conversation
from app.services.export import (
    create_export_job,
    enqueue_export,
    issue_download_token,
    resolve_download,
)
from app.services.iam import Principal
from app.services.trail_writer import trail_aad
from app.services.query import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    TrailFilter,
    list_conversations,
    query_trails,
)

router = APIRouter(prefix="/api/assistant/audit", tags=["assistant-audit"])


class ConversationListItem(BaseModel):
    id: str
    user_id: str
    title: str
    hidden_by_user: bool
    deleted_at: str | None
    created_at: str
    trail_count: int
    first_occurred_at: str | None
    last_occurred_at: str | None
    tool_names: list[str]
    negative_feedback_count: int
    blocked_event_count: int


class ConversationPage(BaseModel):
    items: list[ConversationListItem]
    total: int
    page: int
    page_size: int
    pages: int


class TrailItem(BaseModel):
    id: str
    seq: int
    kind: str
    occurred_at: str
    user_id: str
    conversation_id: str
    hidden_by_user: bool
    content: str | None = None
    content_redacted: str | None = None
    content_sha256: str | None = None
    doc_refs: list[dict[str, Any]] | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    tool_name: str | None = None
    tool_version: str | None = None
    params_redacted: dict[str, Any] | None = None
    result_summary: str | None = None
    result_digest: str | None = None
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    status: str | None = None
    block_reason: str | None = None
    rule_id: str | None = None
    rating: int | None = None
    comment_redacted: str | None = None


class ConversationDetail(BaseModel):
    id: str
    user_id: str
    title: str
    hidden_by_user: bool
    deleted_at: str | None
    created_at: str
    trail_count: int
    trails: list[TrailItem]


class ExportRequest(BaseModel):
    user_id: uuid.UUID | None = None
    from_ts: datetime | None = Field(default=None, alias="from")
    to_ts: datetime | None = Field(default=None, alias="to")
    tool_name: str | None = None
    negative_feedback: bool | None = None
    blocked: bool | None = None
    conversation_id: uuid.UUID | None = None
    kind: str | None = None
    hidden_by_user: bool | None = None

    model_config = {"populate_by_name": True}

    def to_filter(self) -> TrailFilter:
        return TrailFilter(
            user_id=self.user_id,
            from_ts=self.from_ts,
            to_ts=self.to_ts,
            tool_name=self.tool_name,
            negative_feedback=self.negative_feedback,
            blocked=self.blocked,
            conversation_id=self.conversation_id,
            kind=self.kind,
            hidden_by_user=self.hidden_by_user,
        )


class ExportAccepted(BaseModel):
    job_id: str
    status: str
    message: str


class ExportJobStatus(BaseModel):
    job_id: str
    status: str
    format: str
    row_count: int | None = None
    file_sha256: str | None = None
    download_url: str | None = None
    expires_at: str | None = None
    error: str | None = None


def _parse_window(from_ts: datetime | None, to_ts: datetime | None) -> None:
    if from_ts and to_ts and from_ts > to_ts:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_TIME_WINDOW", "message": "from 必须早于或等于 to"},
        )


@router.get("/conversations", response_model=ConversationPage)
def list_audit_conversations(
    request: Request,
    principal: Annotated[Principal, Depends(require_auditor)],
    session: Annotated[Session, Depends(get_session)],
    user_id: uuid.UUID | None = Query(default=None),
    from_ts: datetime | None = Query(default=None, alias="from"),
    to_ts: datetime | None = Query(default=None, alias="to"),
    tool_name: str | None = Query(default=None),
    negative_feedback: bool | None = Query(default=None),
    blocked: bool | None = Query(default=None),
    hidden_by_user: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> ConversationPage:
    """按用户 / 时间 / 工具名 / 是否差评 / 是否被拦截筛选，分页。"""
    _parse_window(from_ts, to_ts)
    filters = TrailFilter(
        user_id=user_id,
        from_ts=from_ts,
        to_ts=to_ts,
        tool_name=tool_name,
        negative_feedback=negative_feedback,
        blocked=blocked,
        hidden_by_user=hidden_by_user,
    )
    items, total = list_conversations(
        session,
        org_id=principal.org_id,
        filters=filters,
        site_ids=principal.site_ids,
        page=page,
        page_size=page_size,
    )
    write_audit_log(
        session,
        AuditEvent(
            org_id=principal.org_id,
            action=ACTION_AUDIT_VIEW,
            target_type="asst_conversation_list",
            target_id=None,
            operator_id=principal.operator_id,
            operator_role=",".join(sorted(principal.roles)),
            ip=client_ip(request),
            site_id=principal.site_ids[0] if principal.site_ids else None,
            detail={
                "filters": filters.as_dict(),
                "page": page,
                "page_size": page_size,
                "returned": len(items),
                "total": total,
            },
        ),
    )
    # 审计记录必须在响应可见前落盘：不能依赖依赖项 teardown 的延后提交
    session.commit()
    return ConversationPage(
        items=[ConversationListItem(**item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, (total + page_size - 1) // page_size),
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_audit_conversation(
    conversation_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_auditor)],
    session: Annotated[Session, Depends(get_session)],
    cipher: Annotated[TrailCipher, Depends(get_cipher)],
) -> ConversationDetail:
    """详情：解密后返回完整内容（含已 hidden_by_user 的会话）。

    跨 org 或越出 site 数据范围一律 404（不用 403，避免成为枚举探针）。
    """
    conversation = get_conversation(
        session, org_id=principal.org_id, conversation_id=conversation_id
    )
    if conversation is None or (
        principal.site_ids and conversation.site_id not in principal.site_ids
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "CONVERSATION_NOT_FOUND", "message": "会话不存在或不在数据范围内"},
        )
    trails = query_trails(
        session,
        org_id=principal.org_id,
        filters=TrailFilter(conversation_id=conversation_id),
        site_ids=principal.site_ids,
    )
    items: list[TrailItem] = []
    for trail in trails:
        content = None
        if trail.content_enc:
            try:
                content = cipher.decrypt(trail.content_enc, aad=trail_aad(trail.id))
            except DecryptionError as exc:
                raise HTTPException(
                    status_code=500,
                    detail={"code": "TRAIL_DECRYPT_FAILED", "message": str(exc)},
                ) from exc
        items.append(
            TrailItem(
                id=str(trail.id),
                seq=trail.seq,
                kind=trail.kind,
                occurred_at=trail.occurred_at.isoformat(),
                user_id=str(trail.user_id),
                conversation_id=str(trail.conversation_id),
                hidden_by_user=trail.hidden_by_user,
                content=content,
                content_redacted=trail.content_redacted,
                content_sha256=trail.content_sha256,
                doc_refs=trail.doc_refs,
                model_name=trail.model_name,
                prompt_version=trail.prompt_version,
                tool_name=trail.tool_name,
                tool_version=trail.tool_version,
                params_redacted=trail.params_redacted,
                result_summary=trail.result_summary,
        result_digest=trail.result_digest,
                confirmed_by=str(trail.confirmed_by) if trail.confirmed_by else None,
                confirmed_at=trail.confirmed_at.isoformat() if trail.confirmed_at else None,
                status=trail.status,
                block_reason=trail.block_reason,
                rule_id=trail.rule_id,
                rating=trail.rating,
                comment_redacted=trail.comment_redacted,
            )
        )
    write_audit_log(
        session,
        AuditEvent(
            org_id=principal.org_id,
            action=ACTION_AUDIT_VIEW,
            target_type="asst_conversation",
            target_id=str(conversation_id),
            operator_id=principal.operator_id,
            operator_role=",".join(sorted(principal.roles)),
            ip=client_ip(request),
            site_id=conversation.site_id,
            detail={"trail_count": len(items), "hidden_by_user": conversation.hidden_by_user},
        ),
    )
    session.commit()
    return ConversationDetail(
        id=str(conversation.id),
        user_id=str(conversation.user_id),
        title=conversation.title,
        hidden_by_user=conversation.hidden_by_user,
        deleted_at=conversation.deleted_at.isoformat() if conversation.deleted_at else None,
        created_at=conversation.created_at.isoformat(),
        trail_count=len(items),
        trails=items,
    )


@router.post(
    "/exports",
    response_model=ExportAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_audit_export(
    payload: ExportRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_auditor)],
    session: Annotated[Session, Depends(get_session)],
    cipher: Annotated[TrailCipher, Depends(get_cipher)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ExportAccepted:
    """异步导出 JSONL：202 + job_id；下载链接 10 分钟有效。"""
    filters = payload.to_filter()
    _parse_window(filters.from_ts, filters.to_ts)
    job = create_export_job(
        session,
        org_id=principal.org_id,
        requested_by=principal.operator_id,
        filters=filters,
        site_id=principal.site_ids[0] if principal.site_ids else None,
    )
    enqueue_export(session=session, job_id=job.id, cipher=cipher, settings=settings)
    write_audit_log(
        session,
        AuditEvent(
            org_id=principal.org_id,
            action=ACTION_AUDIT_EXPORT,
            target_type="asst_export_job",
            target_id=str(job.id),
            operator_id=principal.operator_id,
            operator_role=",".join(sorted(principal.roles)),
            ip=client_ip(request),
            detail={"filters": filters.as_dict(), "format": "jsonl"},
        ),
    )
    session.commit()
    return ExportAccepted(
        job_id=str(job.id),
        status=job.status,
        message="导出任务已受理；用 GET /api/assistant/audit/exports/{job_id} 取下载链接（10 分钟有效）",
    )


@router.get("/exports/{job_id}", response_model=ExportJobStatus)
def get_export_status(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_auditor)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ExportJobStatus:
    """查询导出任务；finished 时签发 10 分钟有效的下载链接。"""
    job = session.get(AsstExportJob, job_id)
    if job is None or job.org_id != principal.org_id:
        raise HTTPException(
            status_code=404,
            detail={"code": "EXPORT_JOB_NOT_FOUND", "message": "导出任务不存在"},
        )
    url = None
    expires_at = None
    if job.status == "finished":
        url, expires_at = issue_download_token(session, job=job, settings=settings)
        session.commit()
    return ExportJobStatus(
        job_id=str(job.id),
        status=job.status,
        format=job.format,
        row_count=job.row_count,
        file_sha256=job.file_sha256,
        download_url=url,
        expires_at=expires_at.isoformat() if expires_at else None,
        error=job.error,
    )


@router.get("/exports/{job_id}/download")
def download_export(
    job_id: uuid.UUID,
    token: str,
    sig: str,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """凭签名令牌下载：单次有效、10 分钟过期；过期 410、重复使用 410。

    下载动作本身也写审计日志（action=export_download）。
    """
    try:
        path = resolve_download(
            session, job_id=job_id, raw_token=token, sig=sig, settings=settings
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "DOWNLOAD_FORBIDDEN", "message": str(exc)},
        ) from exc
    except (FileExistsError, TimeoutError) as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "DOWNLOAD_EXPIRED", "message": str(exc)},
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "EXPORT_FILE_NOT_FOUND", "message": str(exc)},
        ) from exc

    job = session.get(AsstExportJob, job_id)
    write_audit_log(
        session,
        AuditEvent(
            org_id=job.org_id,
            action=ACTION_EXPORT_DOWNLOAD,
            target_type="asst_export_job",
            target_id=str(job_id),
            operator_role="auditor",
            ip=client_ip(request),
            detail={"row_count": job.row_count},
        ),
    )
    session.commit()
    return FileResponse(
        path,
        media_type="application/x-ndjson",
        filename=f"assistant-audit-{job_id}.jsonl",
    )