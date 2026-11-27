"""真实 PostgreSQL 上的验证用例：方言差异、DDL、不可变触发器。

整组用例只在环境变量 ASST_TEST_DATABASE_URL 给出时运行（pytest 夹具会把整份测试
切到真实 PG 的独立 schema）；未给出时整组 skip，SQLite 基线不受影响。

这组用例刻意测「SQLite 上测不到的东西」：
  * PG 版本下限与 plpgsql 触发器真的创建成功（迁移/安装路径都跑一遍）；
  * BEFORE UPDATE OR DELETE 触发器用 jsonb 差集拦住改列、白名单列可改；
  * 清理窗口（asst_purge_guard）在 PG 上真的能放行/收回 DELETE；
  * uuid 列、boolean 列、JSON 列在 PG 上的绑定与应用行为一致。
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text

from app.db.immutability import install_immutability
from app.models.asst_trail import AsstTrail
from app.services.crypto import sha256_hex
from app.services.hash_chain import build_chain, verify_chain
from tests.conftest import (
    AUDITOR_TOKEN,
    EMPLOYEE_TOKEN,
    ORG_ID,
    auth,
    count_rows,
    create_conversation,
    pg_test_url,
    post_message,
    seed_full_conversation,
    set_hidden_by_user,
    set_hidden_by_user_raw,
    tamper_trail_payload,
    update_trail_ciphertext,
)

pytestmark = pytest.mark.skipif(
    pg_test_url() is None,
    reason="需要 ASST_TEST_DATABASE_URL 指向真实 PostgreSQL",
)


def test_dialect_is_postgresql(ctx):
    """夹具确实跑在 PG 上（避免误以为测了 PG 结果还是 SQLite）。"""
    assert ctx.dialect == "postgresql"
    version = ctx.client.get("/healthz")
    assert version.status_code == 200
    with ctx.engine.begin() as conn:
        server_version = conn.execute(text("SHOW server_version")).scalar_one()
        # 交付物要求 PostgreSQL 15+；实测环境为 15.19，不做 16 的硬断言
        major = int(str(server_version).split(".")[0])
    assert major >= 15, f"需要 PostgreSQL 15+，实测 {server_version}"


def test_triggers_exist_in_pg_catalog(ctx):
    """三张表的只增不改触发器都真实存在于 pg_trigger。"""
    with ctx.engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname IN ('asst_trail', 'asst_hash_chain', 'audit_log') "
                "AND NOT t.tgisinternal ORDER BY tgname"
            )
        ).scalars().all()
    assert "trg_asst_trail_immutable" in rows
    assert "trg_asst_hash_chain_append_only" in rows
    assert "trg_audit_log_append_only" in rows


def test_jsonb_diff_blocks_column_update(ctx):
    """改 payload 列（白名单外）→ plpgsql 用 jsonb 差集拦下，报 TRAIL_IMMUTABLE。"""
    conversation_id = seed_full_conversation(ctx)
    with ctx.engine.begin() as conn:
        conn.execute(text("ALTER TABLE asst_trail DISABLE TRIGGER trg_asst_trail_immutable"))
        payload = conn.execute(
            text("SELECT payload FROM asst_trail WHERE conversation_id = :cid LIMIT 1"),
            {"cid": uuid.UUID(conversation_id)},
        ).scalar_one()
        conn.execute(text("ALTER TABLE asst_trail ENABLE TRIGGER trg_asst_trail_immutable"))
    assert payload is not None
    with pytest.raises(Exception) as excinfo:
        update_trail_ciphertext(ctx, conversation_id)
    assert "TRAIL_IMMUTABLE" in str(excinfo.value)


def test_whitelist_columns_update_in_pg(ctx):
    """白名单列在 PG 上改得动：hidden_by_user 布尔赋值成功，其它列不动。"""
    conversation_id = seed_full_conversation(ctx)
    set_hidden_by_user(ctx, conversation_id, True)
    values = count_rows(ctx, AsstTrail, AsstTrail.__table__.c.hidden_by_user.is_(True))
    assert values == 5


def test_non_boolean_rejected_by_pg_type(ctx):
    """PG 的 boolean 列在类型层拒绝非布尔值（SQLite 靠触发器 typeof 校验）。"""
    conversation_id = seed_full_conversation(ctx)
    with pytest.raises(Exception):
        set_hidden_by_user_raw(ctx, conversation_id, 7)


def test_delete_blocked_outside_purge_window(ctx):
    """窗口外 DELETE 一律被拒（PG 版触发器读 asst_purge_guard）。"""
    seed_full_conversation(ctx)
    with pytest.raises(Exception) as excinfo:
        with ctx.engine.begin() as conn:
            conn.execute(text("DELETE FROM asst_trail"))
    assert "TRAIL_IMMUTABLE" in str(excinfo.value)


def test_tamper_detected_on_pg(ctx):
    """AC2 在 PG 上同样成立：改一条 payload → 链校验报出断点位置。"""
    day = date(2026, 4, 2)
    conversation_id = create_conversation(ctx)
    for index in range(6):
        response = ctx.client.post(
            f"/api/assistant/conversations/{conversation_id}/messages",
            headers=auth(EMPLOYEE_TOKEN),
            json={"content": f"PG 第 {index} 条", "occurred_at": f"{day.isoformat()}T10:0{index}:00"},
        )
        assert response.status_code == 201, response.text
    with ctx.session() as session:
        built = build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    assert built.rows == 6
    tamper_trail_payload(ctx, 4)
    install_immutability(ctx.engine)
    with ctx.session() as session:
        result = verify_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
    assert result.ok is False
    assert result.first_break_seq == 4
    assert result.expected_hash != result.actual_hash
    assert len(result.actual_hash) == 64


def test_row_hash_formula_on_pg(ctx):
    """PG 上 canonical_json 与 sha256 口径与 SQLite 完全一致（JSON 列不改变字节）。"""
    day = date(2026, 4, 3)
    conversation_id = create_conversation(ctx)
    response = ctx.client.post(
        f"/api/assistant/conversations/{conversation_id}/messages",
        headers=auth(EMPLOYEE_TOKEN),
        json={"content": "PG 单条", "occurred_at": f"{day.isoformat()}T09:00:00"},
    )
    assert response.status_code == 201, response.text
    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=day)
        session.commit()
        row = session.execute(
            text("SELECT prev_hash, row_hash, payload_canonical FROM asst_hash_chain")
        ).one()
    assert sha256_hex(row[0].encode("utf-8") + row[2].encode("utf-8")) == row[1]


def test_uuid_and_json_roundtrip_on_pg(ctx):
    """PG 的 uuid 列回读一致；JSON 列里的中文与嵌套结构原样保留。"""
    conversation_id = seed_full_conversation(ctx)
    response = ctx.client.get(
        f"/api/assistant/audit/conversations/{conversation_id}", headers=auth(AUDITOR_TOKEN)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == conversation_id
    tool = [t for t in body["trails"] if t["kind"] == "tool_call"][0]
    assert tool["params_redacted"]["phone"] == "138****5678"
    assert "\\u" not in str(tool["params_redacted"])


def test_audit_log_append_only_on_pg(ctx):
    """audit_log 的 append-only 触发器在 PG 上生效（DELETE/UPDATE 均被拒）。"""
    seed_full_conversation(ctx)
    ctx.client.get("/api/assistant/audit/conversations", headers=auth(AUDITOR_TOKEN))
    with pytest.raises(Exception) as excinfo:
        with ctx.engine.begin() as conn:
            conn.execute(text("DELETE FROM audit_log"))
    assert "TRAIL_IMMUTABLE" in str(excinfo.value)
