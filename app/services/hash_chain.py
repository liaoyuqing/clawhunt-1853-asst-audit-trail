"""每日哈希链：计算 + 校验。

口径（与提案 ③ 写死的一致）：
  row_hash = sha256(prev_hash || canonical_json(payload))
  * 链按 (org_id, chain_date) 分链，链首 prev_hash = "0" * 64；
  * canonical_json 为键名排序、UTC 秒级 ISO 时间、浮点转字符串（app/core/canonical.py）；
  * 每天对「前一日」留痕计算，链尾哈希 + 条数写审计日志（app/services/audit_log.py）；
  * 链记录自身存 payload_canonical，所以校验不依赖业务行是否已被保留期清理；
  * 清理掉的段以 AsstPurgeMarker（含 last_purged_row_hash）衔接，校验据此跳过而不误报断点。

校验输出（发现篡改时必须给出断点位置）：
  ok / first_break_seq / break_reason / expected_hash / actual_hash / checked_rows
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.canonical import canonical_json
from app.services.crypto import sha256_hex
from app.db.base import utcnow
from app.models.asst_hash_chain import AsstHashChainEntry, AsstHashChainRun
from app.models.asst_trail import AsstPurgeMarker, AsstTrail
from app.services.audit_log import ACTION_HASH_CHAIN, AuditEvent, write_audit_log

GENESIS_PREV_HASH = "0" * 64

BREAK_TRAIL_MISSING = "trail_row_missing"
BREAK_PAYLOAD_CHANGED = "payload_changed"
BREAK_CHAIN_ENTRY_CHANGED = "chain_entry_changed"
BREAK_PREV_HASH_CHANGED = "prev_hash_changed"
BREAK_SEQ_GAP = "seq_gap"
BREAK_TAIL_CHANGED = "tail_changed"


def compute_row_hash(prev_hash: str, payload_canonical: str) -> str:
    """row_hash = sha256(prev_hash || canonical_json(payload))，prev_hash 为 hex 字符串。"""
    return sha256_hex(prev_hash.encode("utf-8") + payload_canonical.encode("utf-8"))


@dataclass
class ChainResult:
    """一次链计算或校验的结果。字段名直接对应用户可读的报告。"""

    org_id: str
    chain_date: str
    rows: int = 0
    ok: bool = True
    status: str = "ok"
    first_break_seq: int | None = None
    break_reason: str | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None
    chain_head: str | None = None
    chain_tail_hash: str | None = None
    checked_rows: int = 0
    skipped_purged_rows: int = 0
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ordered_trails(session: Session, org_id: uuid.UUID, chain_date: date) -> list[AsstTrail]:
    stmt = (
        select(AsstTrail)
        .where(AsstTrail.org_id == org_id, AsstTrail.chain_date == chain_date)
        .order_by(AsstTrail.seq)
    )
    return list(session.execute(stmt).scalars())


def _markers_for(session: Session, org_id: uuid.UUID) -> list[AsstPurgeMarker]:
    stmt = (
        select(AsstPurgeMarker)
        .where(AsstPurgeMarker.org_id == org_id)
        .order_by(AsstPurgeMarker.purged_from_seq)
    )
    return list(session.execute(stmt).scalars())


def build_chain(
    session: Session,
    *,
    org_id: uuid.UUID,
    chain_date: date,
    write_audit: bool = True,
) -> ChainResult:
    """为 (org, 某日) 计算哈希链；幂等：同一天重跑不产生第二条链。

    已存在链时只对「链上还没有的新行」追加，绝不重算历史行——重算会给篡改者洗白的机会。
    """
    result = ChainResult(org_id=str(org_id), chain_date=chain_date.isoformat())

    existing = _chain_entries(session, org_id, chain_date)
    last_seq = existing[-1].seq if existing else 0
    prev_hash = existing[-1].row_hash if existing else _resume_prev_hash(session, org_id, chain_date)
    trails = [t for t in _ordered_trails(session, org_id, chain_date) if t.seq > last_seq]

    if not trails and existing:
        result.rows = len(existing)
        result.chain_head = existing[0].row_hash
        result.chain_tail_hash = existing[-1].row_hash
        result.message = "当日链已存在，无新增行，未重算历史"
        _upsert_run(
            session,
            org_id=org_id,
            chain_date=chain_date,
            status="ok",
            row_count=len(existing),
            chain_head=result.chain_head,
            chain_tail=result.chain_tail_hash,
        )
        return result

    seq = last_seq
    for trail in trails:
        seq += 1
        payload_canonical = canonical_json(trail.payload)
        row_hash = compute_row_hash(prev_hash, payload_canonical)
        session.add(
            AsstHashChainEntry(
                org_id=org_id,
                chain_date=chain_date,
                seq=seq,
                trail_id=trail.id,
                trail_seq=trail.seq,
                prev_hash=prev_hash,
                row_hash=row_hash,
                payload_sha256=sha256_hex(payload_canonical.encode("utf-8")),
                payload_canonical=payload_canonical,
                occurred_at=trail.occurred_at,
            )
        )
        prev_hash = row_hash
    session.flush()

    entries = _chain_entries(session, org_id, chain_date)
    result.rows = len(entries)
    result.chain_head = entries[0].row_hash
    result.chain_tail_hash = entries[-1].row_hash
    result.checked_rows = len(trails)

    _upsert_run(
        session,
        org_id=org_id,
        chain_date=chain_date,
        status="ok",
        row_count=len(entries),
        chain_head=result.chain_head,
        chain_tail=result.chain_tail_hash,
    )
    if write_audit:
        write_audit_log(
            session,
            AuditEvent(
                org_id=org_id,
                action=ACTION_HASH_CHAIN,
                target_type="asst_hash_chain",
                target_id=f"{org_id}:{chain_date.isoformat()}",
                operator_role="system",
                detail={
                    "chain_date": chain_date.isoformat(),
                    "row_count": result.rows,
                    "tail_hash": result.chain_tail_hash,
                    "head_hash": result.chain_head,
                    "appended": len(trails),
                },
            ),
        )
    return result


def _chain_entries(
    session: Session, org_id: uuid.UUID, chain_date: date
) -> list[AsstHashChainEntry]:
    stmt = (
        select(AsstHashChainEntry)
        .where(AsstHashChainEntry.org_id == org_id, AsstHashChainEntry.chain_date == chain_date)
        .order_by(AsstHashChainEntry.seq)
    )
    return list(session.execute(stmt).scalars())


def _resume_prev_hash(session: Session, org_id: uuid.UUID, chain_date: date) -> str:
    """当日链首的 prev_hash：接在同 org 上一条链（更早日期）的链尾之后。

    更早日期的链可能已被保留期清理整段删掉，此时用 purge_marker 记录的链尾哈希续接，
    否则链首会退回创世哈希，把「到期删除」误报成断点。
    """
    entry = session.execute(
        select(AsstHashChainEntry.row_hash, AsstHashChainEntry.chain_date)
        .where(AsstHashChainEntry.org_id == org_id, AsstHashChainEntry.chain_date < chain_date)
        .order_by(AsstHashChainEntry.chain_date.desc(), AsstHashChainEntry.seq.desc())
        .limit(1)
    ).first()
    marker = session.execute(
        select(AsstPurgeMarker.last_purged_row_hash, AsstPurgeMarker.chain_date)
        .where(
            AsstPurgeMarker.org_id == org_id,
            AsstPurgeMarker.chain_date < chain_date,
            AsstPurgeMarker.last_purged_row_hash.is_not(None),
        )
        .order_by(AsstPurgeMarker.chain_date.desc())
        .limit(1)
    ).first()
    if entry is not None and marker is not None:
        if entry[1] >= marker[1]:
            return entry[0]
        return marker[0]
    if entry is not None:
        return entry[0]
    if marker is not None:
        return marker[0]
    return GENESIS_PREV_HASH


def _upsert_run(
    session: Session,
    *,
    org_id: uuid.UUID,
    chain_date: date,
    status: str,
    row_count: int,
    chain_head: str | None,
    chain_tail: str | None,
    first_break_seq: int | None = None,
    break_reason: str | None = None,
    expected_hash: str | None = None,
    actual_hash: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AsstHashChainRun:
    """同 (org, date) 只保留一条执行记录：再次执行就地复用并更新状态。"""
    stmt = select(AsstHashChainRun).where(
        AsstHashChainRun.org_id == org_id, AsstHashChainRun.chain_date == chain_date
    )
    run = session.execute(stmt).scalar_one_or_none()
    now = utcnow()
    if run is None:
        run = AsstHashChainRun(
            org_id=org_id,
            chain_date=chain_date,
            status=status,
            row_count=row_count,
            chain_head=chain_head,
            chain_tail_hash=chain_tail,
            first_break_seq=first_break_seq,
            break_reason=break_reason,
            expected_hash=expected_hash,
            actual_hash=actual_hash,
            detail=detail or {},
            computed_at=now,
            verified_at=now,
        )
        session.add(run)
    else:
        run.status = status
        run.row_count = row_count
        run.chain_head = chain_head
        run.chain_tail_hash = chain_tail
        run.first_break_seq = first_break_seq
        run.break_reason = break_reason
        run.expected_hash = expected_hash
        run.actual_hash = actual_hash
        run.detail = detail or {}
        run.verified_at = now
    session.flush()
    return run


def verify_chain(
    session: Session,
    *,
    org_id: uuid.UUID,
    chain_date: date,
) -> ChainResult:
    """从链首重算整条链，报告第一个断点位置与 expected/actual 哈希。

    判定顺序（每个链记录）：
      1. 业务行是否还在（被保留期清理按 purge_marker 放行，不算断点）
      2. 业务行 payload 是否被改（payload_changed）
      3. prev_hash 是否等于上一条 row_hash（prev_hash_changed）
      4. 用 prev_hash + payload 重算 row_hash 是否等于落库值（chain_entry_changed）
      5. seq 是否连续（seq_gap）
    """
    entries = _chain_entries(session, org_id, chain_date)
    result = ChainResult(org_id=str(org_id), chain_date=chain_date.isoformat())
    if not entries:
        result.ok = False
        result.status = "no_chain"
        result.message = "该日没有链记录：需要先跑链计算任务"
        return result

    result.chain_head = entries[0].row_hash
    result.chain_tail_hash = entries[-1].row_hash
    result.rows = len(entries)

    markers = _markers_for(session, org_id)
    trail_stmt = select(AsstTrail).where(
        AsstTrail.org_id == org_id, AsstTrail.chain_date == chain_date
    )
    trails_by_seq = {t.seq: t for t in session.execute(trail_stmt).scalars()}

    expected_prev = _resume_prev_hash(session, org_id, chain_date)
    last_seq = 0
    for entry in entries:
        trail = trails_by_seq.get(entry.trail_seq)
        if entry.prev_hash != expected_prev:
            result.ok = False
            result.status = "broken"
            result.first_break_seq = entry.seq
            result.break_reason = BREAK_PREV_HASH_CHANGED
            result.expected_hash = expected_prev
            result.actual_hash = entry.prev_hash
            break
        if entry.seq != last_seq + 1:
            result.ok = False
            result.status = "broken"
            result.first_break_seq = entry.seq
            result.break_reason = BREAK_SEQ_GAP
            result.expected_hash = str(last_seq + 1)
            result.actual_hash = str(entry.seq)
            break
        if trail is None:
            marker = _purge_marker_covering(markers, entry.trail_seq)
            if marker is not None:
                # 被保留期清理删掉的段：跳过，并把期望 prev 推到 marker 记录的链尾哈希
                result.skipped_purged_rows += 1
                expected_prev = marker.last_purged_row_hash or entry.row_hash
                last_seq = entry.seq
                continue
            result.ok = False
            result.status = "broken"
            result.first_break_seq = entry.seq
            result.break_reason = BREAK_TRAIL_MISSING
            result.expected_hash = entry.row_hash
            result.actual_hash = None
            break
        recomputed_payload = canonical_json(trail.payload)
        if recomputed_payload != entry.payload_canonical:
            result.ok = False
            result.status = "broken"
            result.first_break_seq = entry.seq
            result.break_reason = BREAK_PAYLOAD_CHANGED
            result.expected_hash = compute_row_hash(entry.prev_hash, entry.payload_canonical)
            result.actual_hash = compute_row_hash(entry.prev_hash, recomputed_payload)
            break
        recomputed = compute_row_hash(entry.prev_hash, recomputed_payload)
        if recomputed != entry.row_hash:
            result.ok = False
            result.status = "broken"
            result.first_break_seq = entry.seq
            result.break_reason = BREAK_CHAIN_ENTRY_CHANGED
            result.expected_hash = recomputed
            result.actual_hash = entry.row_hash
            break
        expected_prev = entry.row_hash
        last_seq = entry.seq
        result.checked_rows += 1

    _upsert_run(
        session,
        org_id=org_id,
        chain_date=chain_date,
        status=result.status,
        row_count=result.rows,
        chain_head=result.chain_head,
        chain_tail=result.chain_tail_hash,
        first_break_seq=result.first_break_seq,
        break_reason=result.break_reason,
        expected_hash=result.expected_hash,
        actual_hash=result.actual_hash,
        detail={"skipped_purged_rows": result.skipped_purged_rows},
    )
    if result.ok:
        result.message = f"链完整：{result.checked_rows} 条已复核，跳过 {result.skipped_purged_rows} 条已清理行"
    else:
        result.message = (
            f"链在第 {result.first_break_seq} 条断裂（{result.break_reason}）"
        )
    return result


def _purge_marker_covering(
    markers: Sequence[AsstPurgeMarker], trail_seq: int
) -> AsstPurgeMarker | None:
    for marker in markers:
        if marker.purged_from_seq <= trail_seq <= marker.purged_to_seq:
            return marker
    return None


def chain_dates_pending(
    session: Session, org_id: uuid.UUID, as_of: date | None = None
) -> list[date]:
    """列出「有留痕但还没建链」的日期（每日任务的取数口径）。"""
    as_of = as_of or utcnow().date()
    stmt = (
        select(AsstTrail.chain_date)
        .where(AsstTrail.org_id == org_id, AsstTrail.chain_date < as_of)
        .distinct()
        .order_by(AsstTrail.chain_date)
    )
    trail_dates = set(session.execute(stmt).scalars())
    run_stmt = select(AsstHashChainRun.chain_date).where(
        AsstHashChainRun.org_id == org_id,
        AsstHashChainRun.chain_date < as_of,
        AsstHashChainRun.chain_tail_hash.is_not(None),
    )
    built = set(session.execute(run_stmt).scalars())
    return sorted(trail_dates - built)


def daily_build(
    session: Session, org_id: uuid.UUID, as_of: date | None = None
) -> list[ChainResult]:
    """每日任务入口：为所有「前一日及更早、尚未建链」的日期补链。"""
    return [
        build_chain(session, org_id=org_id, chain_date=d)
        for d in chain_dates_pending(session, org_id, as_of)
    ]