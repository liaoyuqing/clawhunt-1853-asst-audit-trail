"""AC4：到期数据清理后，审计日志中有删除摘要。

覆盖：默认 180 天、环境变量改保留期、分批删除、隐藏行同样到期清理、清理后链校验不误报、
清理窗口关闭后 DELETE 恢复全拒、dry-run 只统计不删。
所有直连 SQL 都走 tests/conftest.py 的方言无关辅助函数（PG 上布尔/UUID 按列类型绑定）。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.config import DEFAULT_RETENTION_DAYS
from app.db.base import utcnow
from app.models.asst_hash_chain import AsstHashChainEntry
from app.models.asst_trail import AsstPurgeMarker, AsstTrail
from app.services.audit_log import ACTION_PURGE, query_audit_log
from app.services.hash_chain import build_chain, verify_chain
from app.services.retention import compute_cutoff, purge_all_orgs, run_purge
from tests.conftest import (
    AUDITOR_TOKEN,
    EMPLOYEE_TOKEN,
    ORG_ID,
    auth,
    bulk_delete_trails,
    count_rows,
    create_conversation,
)


def _post_dated_messages(ctx, conversation_id: str, count: int, occurred_at: str) -> None:
    for index in range(count):
        response = ctx.client.post(
            f"/api/assistant/conversations/{conversation_id}/messages",
            headers=auth(EMPLOYEE_TOKEN),
            json={"content": f"历史消息 {index}", "occurred_at": occurred_at},
        )
        assert response.status_code == 201, response.text


def _count_trails(ctx) -> int:
    return count_rows(ctx, AsstTrail)


def test_default_retention_days_env(ctx, monkeypatch):
    """默认 180 天；ASST_LOG_RETENTION_DAYS 可覆盖。"""
    assert DEFAULT_RETENTION_DAYS == 180
    now = datetime(2026, 6, 1, 12, 0, 0)
    assert compute_cutoff(180, now) == datetime(2025, 12, 3, 12, 0, 0)

    monkeypatch.setenv("ASST_LOG_RETENTION_DAYS", "30")
    from app.config import load_settings

    assert load_settings().retention_days == 30


def test_purge_deletes_expired_and_keeps_recent(ctx):
    """AC4 主用例：200 天前 30 条 + 当天 10 条 → 删 30 留 10，审计日志有删除摘要。"""
    now = utcnow()
    old_day = (now - timedelta(days=200)).date()
    conversation_id = create_conversation(ctx)
    _post_dated_messages(ctx, conversation_id, 30, f"{old_day.isoformat()}T09:00:00")
    _post_dated_messages(ctx, conversation_id, 10, now.replace(microsecond=0).isoformat())
    assert _count_trails(ctx) == 40

    with ctx.session() as session:
        summary = run_purge(session, org_id=ORG_ID, now=now)
        session.commit()
    assert summary.deleted_rows == 30
    assert summary.retention_days == 180
    assert summary.range_from is not None and summary.range_to is not None
    assert summary.range_from.date() == old_day

    assert _count_trails(ctx) == 10
    remaining_old = count_rows(ctx, AsstTrail, AsstTrail.__table__.c.occurred_at < summary.cutoff)
    assert remaining_old == 0

    with ctx.session() as session:
        entries = query_audit_log(session, ORG_ID, action=ACTION_PURGE)
    assert len(entries) == 1
    detail = entries[0].detail
    assert detail["count"] == 30
    assert detail["retention_days"] == 180
    assert detail["range_from"] and detail["range_to"]
    assert detail["range_to"].startswith(old_day.isoformat())
    assert detail["chain_tail_hash"] is not None


def test_retention_env_respected(ctx, monkeypatch):
    """把保留期改成 30 天 → 只清 30 天前的行，31 天前的反而不该保留、45 天前的必须清。"""
    monkeypatch.setenv("ASST_LOG_RETENTION_DAYS", "30")
    now = utcnow()
    conversation_id = create_conversation(ctx)
    _post_dated_messages(ctx, conversation_id, 5, f"{(now - timedelta(days=45)).date().isoformat()}T09:00:00")
    _post_dated_messages(ctx, conversation_id, 4, f"{(now - timedelta(days=10)).date().isoformat()}T09:00:00")

    with ctx.session() as session:
        summary = run_purge(session, org_id=ORG_ID, now=now)
        session.commit()
    assert summary.retention_days == 30
    assert summary.deleted_rows == 5
    assert _count_trails(ctx) == 4


def test_purge_is_batched(ctx):
    """按 batch_size 分批删除，批次数可断言（避免长事务）。"""
    now = utcnow()
    old_day = (now - timedelta(days=400)).date()
    conversation_id = create_conversation(ctx)
    _post_dated_messages(ctx, conversation_id, 25, f"{old_day.isoformat()}T09:00:00")
    with ctx.session() as session:
        summary = run_purge(session, org_id=ORG_ID, batch_size=10, now=now)
        session.commit()
    assert summary.deleted_rows == 25
    assert summary.batches == 3
    assert _count_trails(ctx) == 0


def test_hidden_rows_are_purged_too(ctx):
    """隐藏不等于豁免：hidden_by_user 的行到期同样清理（保留期与隐藏无关）。"""
    now = utcnow()
    old_day = (now - timedelta(days=300)).date()
    conversation_id = create_conversation(ctx)
    _post_dated_messages(ctx, conversation_id, 6, f"{old_day.isoformat()}T09:00:00")
    assert (
        ctx.client.delete(
            f"/api/assistant/conversations/{conversation_id}", headers=auth(EMPLOYEE_TOKEN)
        ).status_code
        == 204
    )
    hidden = count_rows(ctx, AsstTrail, AsstTrail.__table__.c.hidden_by_user.is_(True))
    assert hidden == 6

    with ctx.session() as session:
        summary = run_purge(session, org_id=ORG_ID, now=now)
        session.commit()
    assert summary.deleted_rows == 6
    assert _count_trails(ctx) == 0


def test_chain_survives_purge(ctx):
    """清理后链校验不误报断点：删除段由 purge_marker 衔接。"""
    now = utcnow()
    old_day = (now - timedelta(days=200)).date()
    recent_day = (now - timedelta(days=5)).date()
    conversation_id = create_conversation(ctx)
    _post_dated_messages(ctx, conversation_id, 4, f"{old_day.isoformat()}T09:00:00")
    _post_dated_messages(ctx, conversation_id, 3, f"{recent_day.isoformat()}T09:00:00")

    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=old_day)
        build_chain(session, org_id=ORG_ID, chain_date=recent_day)
        session.commit()

    with ctx.session() as session:
        before = verify_chain(session, org_id=ORG_ID, chain_date=recent_day)
        session.commit()
    assert before.ok is True

    with ctx.session() as session:
        summary = run_purge(session, org_id=ORG_ID, now=now)
        session.commit()
    assert summary.deleted_rows == 4

    markers = count_rows(ctx, AsstPurgeMarker)
    with ctx.session() as session:
        after = verify_chain(session, org_id=ORG_ID, chain_date=recent_day)
        session.commit()
    assert markers == 1
    assert after.ok is True
    assert after.skipped_purged_rows == 0  # 旧日链整段删除，不影响新日链


def test_purge_window_closes_after_run(ctx):
    """清理结束后窗口关闭：未到期的行仍然删不掉。"""
    now = utcnow()
    conversation_id = create_conversation(ctx)
    _post_dated_messages(ctx, conversation_id, 2, now.replace(microsecond=0).isoformat())
    with ctx.session() as session:
        run_purge(session, org_id=ORG_ID, now=now)
        session.commit()
    with pytest.raises(Exception) as excinfo:
        bulk_delete_trails(ctx)
    assert "TRAIL_IMMUTABLE" in str(excinfo.value)


def test_dry_run_does_not_delete(ctx):
    now = utcnow()
    old_day = (now - timedelta(days=300)).date()
    conversation_id = create_conversation(ctx)
    _post_dated_messages(ctx, conversation_id, 3, f"{old_day.isoformat()}T09:00:00")
    with ctx.session() as session:
        summary = run_purge(session, org_id=ORG_ID, now=now, dry_run=True)
        session.commit()
    assert summary.deleted_rows == 3
    assert summary.dry_run is True
    assert _count_trails(ctx) == 3
    with ctx.session() as session:
        assert query_audit_log(session, ORG_ID, action=ACTION_PURGE) == []


def test_purge_endpoint_writes_summary(ctx):
    """运维接口也能触发清理并拿到摘要（定时任务的另一种调用方式）。"""
    now = utcnow()
    old_day = (now - timedelta(days=210)).date()
    conversation_id = create_conversation(ctx)
    _post_dated_messages(ctx, conversation_id, 7, f"{old_day.isoformat()}T09:00:00")
    response = ctx.client.post(
        "/api/assistant/ops/retention/purge",
        headers=auth(EMPLOYEE_TOKEN),
        json={},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted_rows"] == 7
    assert body["retention_days"] == 180
    with ctx.session() as session:
        entries = query_audit_log(session, ORG_ID, action=ACTION_PURGE)
    assert len(entries) == 1
    assert entries[0].detail["count"] == 7


def test_invalid_retention_rejected(ctx):
    response = ctx.client.post(
        "/api/assistant/ops/retention/purge",
        headers=auth(EMPLOYEE_TOKEN),
        json={"retention_days": 0},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_RETENTION"


def test_purge_all_orgs_iterates(ctx):
    """定时任务入口遍历所有组织，每组织一条审计日志。"""
    now = utcnow()
    old_day = (now - timedelta(days=300)).date()
    conversation_id = create_conversation(ctx)
    _post_dated_messages(ctx, conversation_id, 2, f"{old_day.isoformat()}T09:00:00")
    with ctx.session() as session:
        summaries = purge_all_orgs(session, now=now)
        session.commit()
    assert len(summaries) == 1
    assert summaries[0].deleted_rows == 2
    with ctx.session() as session:
        entries = query_audit_log(session, ORG_ID, action=ACTION_PURGE)
    assert len(entries) == 1
