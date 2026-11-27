"""AC3：非审计角色调用审计接口 403；审计角色每次查看产生一条审计记录。

覆盖：列表/详情/导出三条路径的角色闸门与审计落库、跨 org 404、site 数据范围、
未认证 401、只认 Bearer 不认 X-Api-Key。
"""
from __future__ import annotations

import uuid

import pytest

from app.services.audit_log import (
    ACTION_AUDIT_EXPORT,
    ACTION_AUDIT_VIEW,
    query_audit_log,
)
from tests.conftest import (
    AUDITOR_ID,
    AUDITOR_TOKEN,
    EMPLOYEE_ID,
    EMPLOYEE_TOKEN,
    ORG_ID,
    OTHER_ORG_AUDITOR_TOKEN,
    SCOPED_AUDITOR_TOKEN,
    SITE_A,
    SITE_B,
    auth,
    create_conversation,
    post_message,
    seed_full_conversation,
)


def test_employee_list_forbidden(ctx):
    """员工角色查列表 → 403 FORBIDDEN_AUDIT_ROLE。"""
    response = ctx.client.get("/api/assistant/audit/conversations", headers=auth(EMPLOYEE_TOKEN))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "FORBIDDEN_AUDIT_ROLE"


def test_employee_detail_forbidden(ctx):
    conversation_id = seed_full_conversation(ctx)
    response = ctx.client.get(
        f"/api/assistant/audit/conversations/{conversation_id}", headers=auth(EMPLOYEE_TOKEN)
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN_AUDIT_ROLE"


def test_employee_export_forbidden(ctx):
    response = ctx.client.post(
        "/api/assistant/audit/exports", headers=auth(EMPLOYEE_TOKEN), json={}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN_AUDIT_ROLE"


def test_unauthenticated_401(ctx):
    assert ctx.client.get("/api/assistant/audit/conversations").status_code == 401
    assert (
        ctx.client.get(
            "/api/assistant/audit/conversations", headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )


def test_x_api_key_header_not_accepted(ctx):
    """鉴权只认 Authorization: Bearer；X-Api-Key 不被接受。"""
    response = ctx.client.get(
        "/api/assistant/audit/conversations", headers={"X-Api-Key": AUDITOR_TOKEN}
    )
    assert response.status_code == 401


def test_auditor_list_writes_one_audit_record(ctx):
    """审计角色查列表 → 200 且恰好新增一条 audit_view 记录（含操作人/IP/筛选）。"""
    seed_full_conversation(ctx)
    before = 0
    with ctx.session() as session:
        before = len(query_audit_log(session, ORG_ID, action=ACTION_AUDIT_VIEW))

    response = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers={**auth(AUDITOR_TOKEN), "X-Forwarded-For": "10.1.2.3"},
        params={"page_size": 10, "tool_name": "crm.member.lookup"},
    )
    assert response.status_code == 200, response.text

    with ctx.session() as session:
        entries = query_audit_log(session, ORG_ID, action=ACTION_AUDIT_VIEW)
    assert len(entries) == before + 1
    latest = entries[-1]
    assert latest.operator_id == AUDITOR_ID
    assert latest.operator_role == "auditor"
    assert latest.ip == "10.1.2.3"
    assert latest.detail["filters"]["tool_name"] == "crm.member.lookup"
    assert latest.detail["total"] >= 1
    assert latest.created_at is not None


def test_auditor_detail_writes_audit_record(ctx):
    """审计角色看详情 → 新增一条 audit_view，target_id 为会话 id。"""
    conversation_id = seed_full_conversation(ctx)
    response = ctx.client.get(
        f"/api/assistant/audit/conversations/{conversation_id}", headers=auth(AUDITOR_TOKEN)
    )
    assert response.status_code == 200
    with ctx.session() as session:
        entries = query_audit_log(session, ORG_ID, action=ACTION_AUDIT_VIEW)
    assert entries[-1].target_id == conversation_id
    assert entries[-1].detail["trail_count"] == 5


def test_every_view_writes_one_record(ctx):
    """每次查看一条记录：连续查 3 次 → 审计记录多 3 条。"""
    conversation_id = seed_full_conversation(ctx)
    for _ in range(3):
        assert (
            ctx.client.get(
                f"/api/assistant/audit/conversations/{conversation_id}",
                headers=auth(AUDITOR_TOKEN),
            ).status_code
            == 200
        )
    with ctx.session() as session:
        entries = query_audit_log(session, ORG_ID, action=ACTION_AUDIT_VIEW)
    assert len(entries) == 3


def test_export_also_logged(ctx):
    """导出同样写审计日志（action=audit_export，含筛选条件与条数）。"""
    seed_full_conversation(ctx)
    response = ctx.client.post(
        "/api/assistant/audit/exports", headers=auth(AUDITOR_TOKEN), json={"blocked": True}
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    with ctx.session() as session:
        entries = query_audit_log(session, ORG_ID, action=ACTION_AUDIT_EXPORT)
    assert len(entries) == 1
    assert entries[0].target_id == job_id
    assert entries[0].detail["filters"]["blocked"] is True
    assert entries[0].detail["format"] == "jsonl"


def test_cross_org_denied_404(ctx):
    """其他 org 的审计角色查本 org 会话 → 404（不是 403，防枚举）。"""
    conversation_id = seed_full_conversation(ctx)
    response = ctx.client.get(
        f"/api/assistant/audit/conversations/{conversation_id}",
        headers=auth(OTHER_ORG_AUDITOR_TOKEN),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "CONVERSATION_NOT_FOUND"


def test_data_scope_filter(ctx):
    """受 site 范围限制的审计角色只看本 site。"""
    in_scope = create_conversation(ctx, title="site A 会话", site_id=SITE_A)
    post_message(ctx, in_scope, "site A 的消息")
    out_scope = create_conversation(ctx, title="site B 会话", site_id=SITE_B)
    post_message(ctx, out_scope, "site B 的消息")

    scoped = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers=auth(SCOPED_AUDITOR_TOKEN),
        params={"page_size": 50},
    )
    assert scoped.status_code == 200, scoped.text
    ids = {item["id"] for item in scoped.json()["items"]}
    assert in_scope in ids
    assert out_scope not in ids

    detail = ctx.client.get(
        f"/api/assistant/audit/conversations/{out_scope}", headers=auth(SCOPED_AUDITOR_TOKEN)
    )
    assert detail.status_code == 404

    full = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers=auth(AUDITOR_TOKEN),
        params={"page_size": 50},
    )
    full_ids = {item["id"] for item in full.json()["items"]}
    assert {in_scope, out_scope} <= full_ids


def test_filters_cover_all_dimensions(ctx):
    """筛选维度齐全：用户、时间、工具名、差评、被拦截。"""
    target = seed_full_conversation(ctx, title="带工具与差评")
    quiet = create_conversation(ctx, title="无工具无差评")
    post_message(ctx, quiet, "普通消息")

    by_tool = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers=auth(AUDITOR_TOKEN),
        params={"tool_name": "crm.member.lookup", "page_size": 50},
    ).json()
    assert [i["id"] for i in by_tool["items"]] == [target]

    by_negative = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers=auth(AUDITOR_TOKEN),
        params={"negative_feedback": "true", "page_size": 50},
    ).json()
    assert [i["id"] for i in by_negative["items"]] == [target]

    by_blocked = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers=auth(AUDITOR_TOKEN),
        params={"blocked": "true", "page_size": 50},
    ).json()
    assert [i["id"] for i in by_blocked["items"]] == [target]

    by_user = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers=auth(AUDITOR_TOKEN),
        params={"user_id": str(EMPLOYEE_ID), "page_size": 50},
    ).json()
    assert {target, quiet} <= {i["id"] for i in by_user["items"]}

    by_time = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers=auth(AUDITOR_TOKEN),
        params={"from": "2000-01-01T00:00:00", "to": "2000-01-02T00:00:00"},
    ).json()
    assert by_time["total"] == 0


def test_pagination(ctx):
    """分页：page_size 生效，总数正确，越界返回空页。"""
    for index in range(3):
        conversation_id = create_conversation(ctx, title=f"会话 {index}")
        post_message(ctx, conversation_id, f"消息 {index}")
    first = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers=auth(AUDITOR_TOKEN),
        params={"page": 1, "page_size": 2},
    ).json()
    assert first["total"] == 3
    assert len(first["items"]) == 2
    assert first["pages"] == 2
    second = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers=auth(AUDITOR_TOKEN),
        params={"page": 2, "page_size": 2},
    ).json()
    assert len(second["items"]) == 1
    assert {i["id"] for i in first["items"]} & {i["id"] for i in second["items"]} == set()


def test_invalid_time_window_400(ctx):
    response = ctx.client.get(
        "/api/assistant/audit/conversations",
        headers=auth(AUDITOR_TOKEN),
        params={"from": "2026-02-01T00:00:00", "to": "2026-01-01T00:00:00"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_TIME_WINDOW"
