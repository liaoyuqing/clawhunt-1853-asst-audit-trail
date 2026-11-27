"""AC2：篡改一条历史留痕后，哈希链校验任务报告断点位置。

覆盖：断点检测（first_break_seq + expected/actual）、链尾写审计日志、同日重跑幂等、
断点后仍可追加、payload 未改但链记录被改也能报出、清理段不误报。

篡改模拟走 tests/conftest.py 的辅助函数（ORM 列类型 + 卸护栏），
因此同一组用例在 SQLite 与真实 PostgreSQL 上都能执行。
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text

from app.services.crypto import sha256_hex
from app.db.immutability import install_immutability
from app.services.hash_chain import (
    GENESIS_PREV_HASH,
    build_chain,
    chain_dates_pending,
    compute_row_hash,
    daily_build,
    verify_chain,
)
from app.services.audit_log import ACTION_HASH_CHAIN, query_audit_log
from tests.conftest import (
    EMPLOYEE_TOKEN,
    ORG_ID,
    create_conversation,
    delete_trail_row,
    post_message,
    tamper_chain_payload_canonical,
    tamper_trail_payload,
)


def _seed(ctx, count: int, *, day: date, conversation_id: str | None = None) -> str:
    """在指定日期写 count 条留痕（occurred_at 落在 day 内）。"""
    conversation_id = conversation_id or create_conversation(ctx)
    for index in range(count):
        response = ctx.client.post(
            f"/api/assistant/conversations/{conversation_id}/messages",
            headers={"Authorization": f"Bearer {EMPLOYEE_TOKEN}"},
            json={
                "content": f"第 {index + 1} 条虚构消息",
                "occurred_at": f"{day.isoformat()}T10:{index % 60:02d}:00",
            },
        )
        assert response.status_code == 201, response.text
    return conversation_id


def _chain_entry_count(ctx) -> int:
    from app.models.asst_hash_chain import AsstHashChainEntry

    from tests.conftest import count_rows

    return count_rows(ctx, AsstHashChainEntry)

def test_chain_builds_and_verifies_clean(ctx):
    """未篡改：链建成、逻辑自洽、链尾写进审计日志。"""
    day = date(2026, 3, 10)
    _seed(ctx, 5, day=day)
    with ctx.session() as session:
        result = build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    assert result.ok and result.rows == 5
    assert result.chain_head and result.chain_tail_hash

    with ctx.session() as session:
        verified = verify_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    assert verified.ok is True
    assert verified.first_break_seq is None
    assert verified.checked_rows == 5


def test_first_break_is_genesis_for_single_day(ctx):
    """单日链的链首 prev_hash 必须是 64 个 0。"""
    day = date(2026, 3, 11)
    _seed(ctx, 2, day=day)
    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
        row = session.execute(
            text("SELECT prev_hash, row_hash FROM asst_hash_chain WHERE seq = 1")
        ).one()
    assert row[0] == GENESIS_PREV_HASH == "0" * 64
    assert len(row[1]) == 64


def test_tamper_detected(ctx):
    """AC2 主用例：改第 23 条 payload → 校验报 first_break_seq=23 + expected/actual 不等。"""
    day = date(2026, 3, 12)
    _seed(ctx, 50, day=day)
    with ctx.session() as session:
        built = build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    assert built.ok and built.rows == 50

    # 模拟「数据库超级用户绕过触发器」：卸掉护栏 → 改 payload → 装回去
    tamper_trail_payload(ctx, 23)
    install_immutability(ctx.engine)

    with ctx.session() as session:
        result = verify_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    assert result.ok is False
    assert result.first_break_seq == 23
    assert result.break_reason == "payload_changed"
    assert result.expected_hash and result.actual_hash
    assert result.expected_hash != result.actual_hash
    assert len(result.expected_hash) == 64 and len(result.actual_hash) == 64


def test_tamper_reported_by_http_verify_endpoint(ctx):
    """同一个断点通过运维接口也能取到（运维/告警用它，不用直连库）。"""
    day = date(2026, 3, 13)
    _seed(ctx, 5, day=day)
    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()

    tamper_trail_payload(ctx, 3)
    install_immutability(ctx.engine)

    response = ctx.client.post(
        "/api/assistant/ops/hash-chain/verify",
        headers={"Authorization": f"Bearer {EMPLOYEE_TOKEN}"},
        json={"chain_date": day.isoformat()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["first_break_seq"] == 3
    assert body["expected_hash"] != body["actual_hash"]


def test_chain_tail_written_to_audit(ctx):
    """链尾哈希 + 条数写审计日志（action=hash_chain）。"""
    day = date(2026, 3, 14)
    _seed(ctx, 50, day=day)
    with ctx.session() as session:
        result = build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
        entries = query_audit_log(session, org_id=ORG_ID, action=ACTION_HASH_CHAIN)
    assert len(entries) == 1
    detail = entries[0].detail
    assert detail["row_count"] == 50
    assert detail["tail_hash"] == result.chain_tail_hash
    assert detail["chain_date"] == day.isoformat()
    assert detail["head_hash"] == result.chain_head


def test_rerun_idempotent(ctx):
    """同一天重跑链任务不产生第二条链记录、不重算历史行。"""
    day = date(2026, 3, 15)
    _seed(ctx, 10, day=day)
    with ctx.session() as session:
        first = build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
        tail_first = first.chain_tail_hash
        second = build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
        count = _chain_entry_count(ctx)
        runs = session.execute(text("SELECT count(*) FROM asst_hash_chain_run")).scalar_one()
    assert count == 10
    assert runs == 1
    assert second.chain_tail_hash == tail_first
    assert second.rows == 10


def test_rerun_after_tamper_does_not_whiten(ctx):
    """篡改后重跑链任务不会把断点洗白（只追加新行、绝不重算历史行）。"""
    day = date(2026, 3, 16)
    conversation_id = _seed(ctx, 4, day=day)
    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()

    tamper_trail_payload(ctx, 2)
    install_immutability(ctx.engine)

    _seed(ctx, 1, day=day, conversation_id=conversation_id)
    with ctx.session() as session:
        rebuilt = build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
        verified = verify_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    assert rebuilt.rows == 5
    assert verified.ok is False
    assert verified.first_break_seq == 2


def test_append_after_break(ctx):
    """断点之后仍能追加留痕（不因历史断点阻塞写入）。"""
    day = date(2026, 3, 17)
    conversation_id = _seed(ctx, 3, day=day)
    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    tamper_trail_payload(ctx, 1, value="X")
    install_immutability(ctx.engine)
    _seed(ctx, 2, day=day, conversation_id=conversation_id)
    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
        count = session.execute(text("SELECT count(*) FROM asst_trail")).scalar_one()
    assert count == 5


def test_chain_entry_tamper_detected(ctx):
    """不改业务行、只改链记录本身 → 报 chain_entry_changed。"""
    day = date(2026, 3, 18)
    _seed(ctx, 5, day=day)
    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    tamper_chain_payload_canonical(ctx, 4)
    install_immutability(ctx.engine)
    with ctx.session() as session:
        result = verify_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    assert result.ok is False
    assert result.first_break_seq == 4
    assert result.break_reason in {"payload_changed", "chain_entry_changed"}


def test_missing_trail_without_marker_detected(ctx):
    """删掉一条留痕但没有清理标记 → 报 trail_row_missing（不是「没事」）。"""
    day = date(2026, 3, 19)
    _seed(ctx, 4, day=day)
    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    delete_trail_row(ctx, 2)
    install_immutability(ctx.engine)
    with ctx.session() as session:
        result = verify_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    assert result.ok is False
    assert result.first_break_seq == 2
    assert result.break_reason == "trail_row_missing"


def test_multi_day_chains_link(ctx):
    """跨日链：后一日的链首 prev_hash 接在前一日链尾之后。"""
    day1 = date(2026, 3, 20)
    day2 = day1 + timedelta(days=1)
    _seed(ctx, 3, day=day1)
    _seed(ctx, 2, day=day2)
    with ctx.session() as session:
        first = build_chain(session, org_id=ORG_ID, chain_date=day1)
        second = build_chain(session, org_id=ORG_ID, chain_date=day2)
        session.commit()
        head2 = session.execute(
            text("SELECT prev_hash FROM asst_hash_chain WHERE chain_date = :d AND seq = 1"),
            {"d": day2.isoformat()},
        ).scalar_one()
        verified1 = verify_chain(session, org_id=ORG_ID, chain_date=day1)
        verified2 = verify_chain(session, org_id=ORG_ID, chain_date=day2)
        session.commit()
    assert head2 == first.chain_tail_hash
    assert verified1.ok and verified2.ok


def test_daily_build_covers_pending_dates(ctx):
    """每日任务：为所有「有留痕且尚未建链」的日期补链（昨日及更早）。"""
    day1 = date(2026, 3, 22)
    day2 = date(2026, 3, 23)
    _seed(ctx, 2, day=day1)
    _seed(ctx, 3, day=day2)
    with ctx.session() as session:
        pending = chain_dates_pending(session, ORG_ID, as_of=date(2026, 3, 24))
        results = daily_build(session, ORG_ID, as_of=date(2026, 3, 24))
        session.commit()
    assert pending == [day1, day2]
    assert [r.rows for r in results] == [2, 3]
    with ctx.session() as session:
        assert chain_dates_pending(session, ORG_ID, as_of=date(2026, 3, 24)) == []


def test_row_hash_formula_matches_spec(ctx):
    """row_hash = sha256(prev_hash || canonical_json(payload))，逐字节对齐公式。"""
    day = date(2026, 3, 24)
    _seed(ctx, 1, day=day)
    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
        entry = session.execute(
            text("SELECT prev_hash, row_hash, payload_canonical FROM asst_hash_chain")
        ).one()
    expected = sha256_hex(entry[0].encode("utf-8") + entry[2].encode("utf-8"))
    assert expected == entry[1]
    assert compute_row_hash(entry[0], entry[2]) == entry[1]
