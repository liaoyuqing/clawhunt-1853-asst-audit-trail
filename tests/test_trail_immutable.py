"""AC1：用户删除会话后，审计接口仍能查到该会话完整内容。

另外覆盖提案承诺的「只增不改」硬约束：DB 触发器拒绝改密文/改判据、原文不落明文。
"""
from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import text

from tests.conftest import (
    AUDITOR_TOKEN,
    EMPLOYEE_TOKEN,
    auth,
    create_conversation,
    post_answer,
    post_message,
    seed_full_conversation,
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
    with ctx.session() as session:
        before = session.execute(
            text("SELECT count(*) FROM asst_trail WHERE conversation_id = :cid"),
            {"cid": conversation_id.replace("-", "")},
        ).scalar_one()
    assert before == 5

    assert (
        ctx.client.delete(
            f"/api/assistant/conversations/{conversation_id}", headers=auth(EMPLOYEE_TOKEN)
        ).status_code
        == 204
    )

    with ctx.session() as session:
        after = session.execute(
            text("SELECT count(*) FROM asst_trail WHERE conversation_id = :cid"),
            {"cid": conversation_id.replace("-", "")},
        ).scalar_one()
    assert after == before


def test_db_trigger_blocks_update(ctx):
    """直接 UPDATE 留痕内容 → DB 抛 TRAIL_IMMUTABLE（应用层之外的第二道闸）。"""
    conversation_id = seed_full_conversation(ctx)
    with pytest.raises(Exception) as excinfo:
        with ctx.engine.begin() as conn:
            conn.execute(
                text("UPDATE asst_trail SET content_redacted = 'tampered' WHERE conversation_id = :cid"),
                {"cid": conversation_id.replace("-", "")},
            )
    assert "TRAIL_IMMUTABLE" in str(excinfo.value)


def test_db_trigger_blocks_delete_without_purge_window(ctx):
    """没有清理窗口时 DELETE 一律被拒。"""
    seed_full_conversation(ctx)
    with pytest.raises(Exception) as excinfo:
        with ctx.engine.begin() as conn:
            conn.execute(text("DELETE FROM asst_trail"))
    assert "TRAIL_IMMUTABLE" in str(excinfo.value)


def test_hidden_flag_only_whitelist(ctx):
    """白名单两列可改；其它列一律拒（含试图把 hidden_by_user 改成非布尔）。"""
    conversation_id = seed_full_conversation(ctx)
    with ctx.engine.begin() as conn:
        conn.execute(
            text("UPDATE asst_trail SET hidden_by_user = 1 WHERE conversation_id = :cid"),
            {"cid": conversation_id.replace("-", "")},
        )
    with pytest.raises(Exception) as excinfo:
        with ctx.engine.begin() as conn:
            conn.execute(
                text("UPDATE asst_trail SET hidden_by_user = 7 WHERE conversation_id = :cid"),
                {"cid": conversation_id.replace("-", "")},
            )
    assert "TRAIL_IMMUTABLE" in str(excinfo.value)


def test_encrypted_at_rest(ctx):
    """原文列不是明文：内容不出现原文，且带 enc:v1: 前缀；脱敏列已掩码。"""
    conversation_id = create_conversation(ctx)
    secret = "会员 13812345678 的对接记录"
    post_message(ctx, conversation_id, secret)
    with ctx.session() as session:
        row = session.execute(
            text("SELECT content_enc, content_redacted FROM asst_trail WHERE conversation_id = :cid"),
            {"cid": conversation_id.replace("-", "")},
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
