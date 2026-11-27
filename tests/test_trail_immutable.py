"""AC1：用户删除会话后，审计接口仍能查到该会话完整内容。

另外覆盖提案承诺的「只增不改」硬约束：DB 触发器拒绝改密文/改判据、原文不落明文。
所有直连 SQL 都走 tests/conftest.py 的方言无关辅助函数，因此同一组用例在
SQLite 与真实 PostgreSQL 上都能跑（PG 上布尔列类型校验由数据库本身兜底）。
"""
from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.models.asst_trail import AsstTrail
from tests.conftest import (
    AUDITOR_TOKEN,
    EMPLOYEE_TOKEN,
    auth,
    bulk_delete_trails,
    count_rows,
    create_conversation,
    post_answer,
    post_message,
    post_tool_call,
    seed_full_conversation,
    set_hidden_by_user,
    set_hidden_by_user_raw,
    update_trail_ciphertext,
)


def _trail_count(ctx, conversation_id: str) -> int:
    return count_rows(
        ctx, AsstTrail, AsstTrail.__table__.c.conversation_id == uuid.UUID(conversation_id)
    )


def test_hidden_but_queryable(ctx):
    """AC1 主用例：删除会话 → 审计详情仍返回 200 且内容逐条一致。"""
    conversation_id = create_conversation(ctx, title="3 轮问答")
    originals = [
        "帮我查一下会员 A 的对接进度 13812345678",
        "按虚构制度 v3，该会员可对接。",
        "再确认下上次对接时间",
    ]
    for content in originals:
        post_message(ctx, conversation_id, content)
        post_answer(ctx, conversation_id, f"回答：{content}")

    before = ctx.client.get(
        f"/api/assistant/audit/conversations/{conversation_id}", headers=auth(AUDITOR_TOKEN)
    )
    assert before.status_code == 200, before.text
    before_body = before.json()
    assert before_body["trail_count"] == 6

    delete = ctx.client.delete(
        f"/api/assistant/conversations/{conversation_id}", headers=auth(EMPLOYEE_TOKEN)
    )
    assert delete.status_code == 204, delete.text

    after = ctx.client.get(
        f"/api/assistant/audit/conversations/{conversation_id}", headers=auth(AUDITOR_TOKEN)
    )
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["hidden_by_user"] is True
    assert body["trail_count"] == 6

    user_messages = [t for t in body["trails"] if t["kind"] == "message"]
    assert [m["content"] for m in user_messages] == originals
    for item in body["trails"]:
        if item["content"] is not None:
            assert hashlib.sha256(item["content"].encode("utf-8")).hexdigest() == item[
                "content_sha256"
            ]


def test_delete_does_not_remove_rows(ctx):
    """删除会话后留痕行数不变（不是软删留痕，是根本没删）。"""
    conversation_id = seed_full_conversation(ctx)
    before = _trail_count(ctx, conversation_id)
    assert before == 5

    assert (
        ctx.client.delete(
            f"/api/assistant/conversations/{conversation_id}", headers=auth(EMPLOYEE_TOKEN)
        ).status_code
        == 204
    )

    assert _trail_count(ctx, conversation_id) == before


def test_db_trigger_blocks_update(ctx):
    """直接 UPDATE 留痕内容 → DB 抛 TRAIL_IMMUTABLE（应用层之外的第二道闸）。"""
    conversation_id = seed_full_conversation(ctx)
    with pytest.raises(Exception) as excinfo:
        update_trail_ciphertext(ctx, conversation_id)
    assert "TRAIL_IMMUTABLE" in str(excinfo.value)


def test_db_trigger_blocks_delete_without_purge_window(ctx):
    """没有清理窗口时 DELETE 一律被拒。"""
    seed_full_conversation(ctx)
    with pytest.raises(Exception) as excinfo:
        bulk_delete_trails(ctx)
    assert "TRAIL_IMMUTABLE" in str(excinfo.value)


def test_hidden_flag_only_whitelist(ctx):
    """白名单两列可改；其它列一律拒。

    非布尔取值这一条：SQLite 里列类型宽松，靠触发器里的 typeof 校验拦住；
    PostgreSQL 的 boolean 列本身不接受非布尔值，类型层就会报错。两种方言都拒绝，错误文案不同。
    """
    conversation_id = seed_full_conversation(ctx)
    set_hidden_by_user(ctx, conversation_id, True)  # 白名单列：改得动
    with ctx.engine.begin() as conn:
        values = conn.execute(
            select(AsstTrail.__table__.c.hidden_by_user).where(
                AsstTrail.__table__.c.conversation_id == uuid.UUID(conversation_id)
            )
        ).scalars().all()
    assert values and all(values)

    # 非布尔取值：SQLite 列类型宽松，靠触发器 typeof 校验拦住；
    # PostgreSQL 的 boolean 列在类型层就拒绝。两条路都是「写不进去」。
    with pytest.raises(Exception) as excinfo:
        set_hidden_by_user_raw(ctx, conversation_id, 7)
    message = str(excinfo.value)
    assert "TRAIL_IMMUTABLE" in message or "boolean" in message.lower()


def test_encrypted_at_rest(ctx):
    """原文列不是明文：内容不出现原文，且带 enc:v1: 前缀；脱敏列已掩码。"""
    conversation_id = create_conversation(ctx)
    secret = "会员 13812345678 的对接记录"
    post_message(ctx, conversation_id, secret)
    with ctx.session() as session:
        row = session.execute(
            select(AsstTrail.content_enc, AsstTrail.content_redacted).where(
                AsstTrail.conversation_id == uuid.UUID(conversation_id)
            )
        ).one()
    assert row[0].startswith("enc:v1:")
    assert secret not in row[0]
    assert "13812345678" not in (row[1] or "")
    assert "138****5678" in (row[1] or "")


def test_audit_list_shows_hidden_conversation(ctx):
    """列表接口同样能查到已隐藏会话，并给出 hidden_by_user=true。"""
    conversation_id = seed_full_conversation(ctx)
    ctx.client.delete(
        f"/api/assistant/conversations/{conversation_id}", headers=auth(EMPLOYEE_TOKEN)
    )
    response = ctx.client.get(
        "/api/assistant/audit/conversations", headers=auth(AUDITOR_TOKEN), params={"page_size": 50}
    )
    assert response.status_code == 200, response.text
    ids = [item["id"] for item in response.json()["items"]]
    assert conversation_id in ids
    matched = [i for i in response.json()["items"] if i["id"] == conversation_id][0]
    assert matched["hidden_by_user"] is True


def test_unknown_conversation_404(ctx):
    response = ctx.client.get(
        f"/api/assistant/audit/conversations/{uuid.uuid4()}", headers=auth(AUDITOR_TOKEN)
    )
    assert response.status_code == 404


def test_tool_call_keeps_result_digest(ctx):
    """提案 ⑤ 字段清单：工具调用留痕含 result_digest（结果摘要的 sha256，可离线核对）。"""
    conversation_id = create_conversation(ctx, title="工具调用字段")
    post_tool_call(ctx, conversation_id, tool_name="crm.member.lookup")
    response = ctx.client.get(
        f"/api/assistant/audit/conversations/{conversation_id}", headers=auth(AUDITOR_TOKEN)
    )
    assert response.status_code == 200, response.text
    tool = [t for t in response.json()["trails"] if t["kind"] == "tool_call"][0]
    assert tool["tool_name"] == "crm.member.lookup"
    assert tool["tool_version"] == "1.4.2"
    assert tool["params_redacted"]["phone"] == "138****5678"
    assert "13812345678" not in tool["result_summary"]
    expected = hashlib.sha256(
        "命中 1 条虚构会员记录，联系人 13812345678".encode("utf-8")
    ).hexdigest()
    assert tool["result_digest"] == expected
    from tests.conftest import CONFIRMER_ID

    assert tool["confirmed_by"] == str(CONFIRMER_ID)
    assert tool["confirmed_at"] is not None
    assert tool["status"] == "ok"
