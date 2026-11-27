"""S2-AUDIT 对接点：审计日志写入与查询。

本单自带一个最小落库实现（audit_log 表 + 只增不改触发器）。
真实环境把 `write_audit_log` / `query_audit_log` 换成 S2-AUDIT 的客户端即可，
调用方签名与返回结构不变；README 有逐字段对照表。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.audit_log import AuditLogEntry

ACTION_AUDIT_VIEW = "audit_view"
ACTION_AUDIT_EXPORT = "audit_export"
ACTION_EXPORT_DOWNLOAD = "export_download"
ACTION_HASH_CHAIN = "hash_chain"
ACTION_PURGE = "purge"


@dataclass(frozen=True)
class AuditEvent:
    """审计事件：字段与 S2-AUDIT 的写入契约一一对应（见 README 对接表）。"""

    org_id: uuid.UUID
    action: str
    site_id: uuid.UUID | None = None
    target_type: str | None = None
    target_id: str | None = None
    operator_id: uuid.UUID | None = None
    operator_role: str | None = None
    ip: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


def write_audit_log(session: Session, event: AuditEvent) -> AuditLogEntry:
    """写一条审计日志。审计表自身只增不改，写入不依赖调用方事务的后续提交。"""
    entry = AuditLogEntry(
        org_id=event.org_id,
        site_id=event.site_id,
        action=event.action,
        target_type=event.target_type,
        target_id=event.target_id,
        operator_id=event.operator_id,
        operator_role=event.operator_role,
        ip=event.ip,
        detail=event.detail or {},
        created_at=event.created_at or utcnow(),
        created_by=event.operator_id,
    )
    session.add(entry)
    session.flush()
    return entry


def query_audit_log(
    session: Session,
    org_id: uuid.UUID,
    action: str | None = None,
    target_id: str | None = None,
) -> list[AuditLogEntry]:
    """按组织 + 动作 + 目标查询（测试与运维排查用）。"""
    stmt = select(AuditLogEntry).where(AuditLogEntry.org_id == org_id)
    if action is not None:
        stmt = stmt.where(AuditLogEntry.action == action)
    if target_id is not None:
        stmt = stmt.where(AuditLogEntry.target_id == target_id)
    stmt = stmt.order_by(AuditLogEntry.created_at)
    return list(session.execute(stmt).scalars())
