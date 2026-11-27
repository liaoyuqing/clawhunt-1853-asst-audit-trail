"""运维接口：链计算 / 链校验 / 保留期清理（供定时任务或运维手动触发）。

生产由 cron / Airflow 调这些 HTTP 端点，或用 `python -m app.workers.*` 直接跑；
两边调的是同一套服务层函数。
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_principal, get_session
from app.services.hash_chain import ChainResult, build_chain, daily_build, verify_chain
from app.services.iam import Principal
from app.services.retention import PurgeSummary, run_purge

router = APIRouter(prefix="/api/assistant/ops", tags=["assistant-ops"])


class ChainRequest(BaseModel):
    chain_date: date | None = None


class VerifyRequest(BaseModel):
    chain_date: date


class PurgeRequest(BaseModel):
    retention_days: int | None = None
    batch_size: int | None = None
    dry_run: bool = False


@router.post("/hash-chain/build")
def build(
    payload: ChainRequest,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    """为指定日期（缺省=前一日）计算哈希链；同一天重跑不产生第二条链。"""
    if payload.chain_date is None:
        results = daily_build(session, principal.org_id)
        session.commit()
        return {"built": [r.as_dict() for r in results]}
    result = build_chain(session, org_id=principal.org_id, chain_date=payload.chain_date)
    session.commit()
    return result.as_dict()


@router.post("/hash-chain/verify")
def verify(
    payload: VerifyRequest,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    """从链首重算，报告是否完整与断点位置（first_break_seq / expected / actual）。"""
    result = verify_chain(session, org_id=principal.org_id, chain_date=payload.chain_date)
    session.commit()
    return result.as_dict()


@router.post("/retention/purge")
def purge(
    payload: PurgeRequest,
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    """执行保留期清理（默认 180 天），返回摘要并写审计日志。"""
    try:
        summary = run_purge(
            session,
            org_id=principal.org_id,
            retention_days=payload.retention_days,
            batch_size=payload.batch_size,
            dry_run=payload.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_RETENTION", "message": str(exc)},
        ) from exc
    session.commit()
    return summary.as_dict()
