"""保留期清理任务：到期留痕物理删除 + 审计日志删除摘要。

口径（提案 ③ 写死）：
  * 保留期 = ASST_LOG_RETENTION_DAYS（默认 180 天）；
  * 清理条件 created_at/occurred_at < now - retention，**与是否 hidden 无关**（隐藏不等于到期）；
  * 物理删除只在本函数的「清理窗口」内进行：触发器只放行 occurred_at < cutoff 的行；
  * 按 ASST_PURGE_BATCH_SIZE 分批（默认 1000），避免长事务；
  * 每批删掉的留痕连同其哈希链记录一起删，并写一条 AsstPurgeMarker（含被删段链尾哈希），
    使链校验能把「到期删除」与「篡改」区分开；
  * 一次清理写一条审计日志（action=purge）：条数、时间范围、链尾哈希。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import DEFAULT_RETENTION_DAYS, load_settings
from app.db.base import utcnow
from app.db.immutability import close_purge_window, open_purge_window
from app.models.asst_hash_chain import AsstHashChainEntry
from app.models.asst_trail import AsstPurgeMarker, AsstTrail
from app.services.audit_log import ACTION_PURGE, AuditEvent, write_audit_log
from app.services.trail_writer import payload_digest


@dataclass
class PurgeSummary:
    """一次清理的结果：审计日志 detail 与测试断言都用这组字段。"""

    org_id: str
    retention_days: int
    cutoff: datetime
    deleted_rows: int = 0
    deleted_chain_entries: int = 0
    batches: int = 0
    range_from: datetime | None = None
    range_to: datetime | None = None
    chain_tail_hash: str | None = None
    audit_log_id: str | None = None
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "retention_days": self.retention_days,
            "cutoff": self.cutoff.isoformat(),
            "deleted_rows": self.deleted_rows,
            "deleted_chain_entries": self.deleted_chain_entries,
            "batches": self.batches,
            "range_from": self.range_from.isoformat() if self.range_from else None,
            "range_to": self.range_to.isoformat() if self.range_to else None,
            "chain_tail_hash": self.chain_tail_hash,
            "audit_log_id": self.audit_log_id,
        }


def compute_cutoff(
    retention_days: int | None = None, now: datetime | None = None
) -> datetime:
    """到期线：now - retention_days。保留期 <= 0 视为非法，避免误删全部。"""
    days = DEFAULT_RETENTION_DAYS if retention_days is None else retention_days
    if days <= 0:
        raise ValueError("保留期必须是正整数天")
    return (now or utcnow()) - timedelta(days=days)


def resolve_retention_days(explicit: int | None = None) -> int:
    """优先用显式入参，其次 ASST_LOG_RETENTION_DAYS，最后默认 180。"""
    if explicit is not None:
        return explicit
    return load_settings().retention_days


def run_purge(
    session: Session,
    *,
    org_id: uuid.UUID,
    retention_days: int | None = None,
    batch_size: int | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> PurgeSummary:
    """对一个组织执行保留期清理；返回摘要并已写审计日志。

    dry_run=True 只统计不删除（运维预演）。
    """
    days = resolve_retention_days(retention_days)
    cutoff = compute_cutoff(days, now)
    size = batch_size or load_settings().purge_batch_size
    summary = PurgeSummary(
        org_id=str(org_id), retention_days=days, cutoff=cutoff, dry_run=dry_run
    )

    rows = list(
        session.execute(
            select(AsstTrail)
            .where(AsstTrail.org_id == org_id, AsstTrail.occurred_at < cutoff)
            .order_by(AsstTrail.seq)
        ).scalars()
    )
    if not rows:
        summary.chain_tail_hash = _current_tail_hash(session, org_id)
        return summary

    summary.range_from = rows[0].occurred_at
    summary.range_to = rows[-1].occurred_at
    summary.deleted_rows = len(rows)
    if dry_run:
        summary.batches = (len(rows) + size - 1) // size
        return summary

    deleted_chain = 0
    last_row_hash: str | None = None
    # 清理窗口与删除在同一事务：窗口关闭后 DELETE 立刻恢复全拒
    open_purge_window(session.connection(), cutoff)
    try:
        for offset in range(0, len(rows), size):
            chunk = rows[offset : offset + size]
            seqs = [r.seq for r in chunk]
            # 没有链记录时（清理跑在链任务之前）也要留下一个可复核的尾指纹
            last_payload_digest = payload_digest(chunk[-1].payload)
            chain_rows = session.execute(
                select(AsstHashChainEntry)
                .where(
                    AsstHashChainEntry.org_id == org_id,
                    AsstHashChainEntry.trail_seq.in_(seqs),
                )
                .order_by(AsstHashChainEntry.seq)
            ).scalars().all()
            if chain_rows:
                deleted_chain += session.execute(
                    delete(AsstHashChainEntry).where(
                        AsstHashChainEntry.id.in_([c.id for c in chain_rows])
                    )
                ).rowcount or 0
                last_row_hash = chain_rows[-1].row_hash
                by_date: dict[date, list[AsstHashChainEntry]] = {}
                for entry in chain_rows:
                    by_date.setdefault(entry.chain_date, []).append(entry)
                for chain_date, entries in by_date.items():
                    session.add(
                        AsstPurgeMarker(
                            org_id=org_id,
                            chain_date=chain_date,
                            purged_from_seq=min(e.trail_seq for e in entries),
                            purged_to_seq=max(e.trail_seq for e in entries),
                            chain_seq_from=min(e.seq for e in entries),
                            chain_seq_to=max(e.seq for e in entries),
                            row_count=len(entries),
                            last_purged_row_hash=entries[-1].row_hash,
                            purged_at=utcnow(),
                        )
                    )
            session.execute(
                delete(AsstTrail).where(
                    AsstTrail.org_id == org_id, AsstTrail.seq.in_(seqs)
                )
            )
            if last_row_hash is None:
                last_row_hash = last_payload_digest
            summary.batches += 1
            session.flush()
    finally:
        close_purge_window(session.connection())

    summary.deleted_chain_entries = deleted_chain
    summary.chain_tail_hash = last_row_hash or _current_tail_hash(session, org_id)

    entry = write_audit_log(
        session,
        AuditEvent(
            org_id=org_id,
            action=ACTION_PURGE,
            target_type="asst_trail",
            target_id=str(org_id),
            operator_role="system",
            detail={
                "count": summary.deleted_rows,
                "retention_days": days,
                "cutoff": cutoff.isoformat(),
                "range_from": summary.range_from.isoformat() if summary.range_from else None,
                "range_to": summary.range_to.isoformat() if summary.range_to else None,
                "chain_tail_hash": summary.chain_tail_hash,
                "chain_entries_deleted": summary.deleted_chain_entries,
                "batches": summary.batches,
            },
        ),
    )
    summary.audit_log_id = str(entry.id)
    return summary


def _current_tail_hash(session: Session, org_id: uuid.UUID) -> str | None:
    """组织当前链尾哈希（按 chain_date + seq 取最后一条）。"""
    stmt = (
        select(AsstHashChainEntry.row_hash)
        .where(AsstHashChainEntry.org_id == org_id)
        .order_by(AsstHashChainEntry.chain_date.desc(), AsstHashChainEntry.seq.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def purge_all_orgs(
    session: Session,
    *,
    retention_days: int | None = None,
    batch_size: int | None = None,
    now: datetime | None = None,
) -> list[PurgeSummary]:
    """定时任务入口：遍历所有组织各清一次（每组织一条审计日志）。"""
    org_ids = session.execute(select(AsstTrail.org_id).distinct()).scalars().all()
    return [
        run_purge(
            session,
            org_id=org_id,
            retention_days=retention_days,
            batch_size=batch_size,
            now=now,
        )
        for org_id in org_ids
    ]