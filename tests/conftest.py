"""测试夹具：临时 SQLite 库 + FastAPI TestClient + 固定虚构身份。

测试不需要外部 PostgreSQL / Redis：数据库用临时文件 SQLite（触发器与不可变约束是同一套 DDL
的 SQLite 版本），导出队列用 inline。README「验证边界」章节逐条写明与 PG16 的差异。
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

TEST_TRAIL_KEY = "unit-test-only-key-0000000000000000000000000000"

ORG_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
SITE_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SITE_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
EMPLOYEE_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
AUDITOR_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
SCOPED_AUDITOR_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
CONFIRMER_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")

EMPLOYEE_TOKEN = "stub-token-employee"
AUDITOR_TOKEN = "stub-token-auditor"
SCOPED_AUDITOR_TOKEN = "stub-token-auditor-site-a"
OTHER_ORG_AUDITOR_TOKEN = "stub-token-auditor-other-org"


def _stub_tokens() -> str:
    return ";".join(
        [
            f"{EMPLOYEE_TOKEN}:{EMPLOYEE_ID}:{ORG_ID}:employee:{SITE_A}",
            f"{AUDITOR_TOKEN}:{AUDITOR_ID}:{ORG_ID}:auditor:",
            f"{SCOPED_AUDITOR_TOKEN}:{SCOPED_AUDITOR_ID}:{ORG_ID}:auditor:{SITE_A}",
            f"{OTHER_ORG_AUDITOR_TOKEN}:{AUDITOR_ID}:{OTHER_ORG_ID}:auditor:",
        ]
    )


@dataclass
class TestContext:
    client: TestClient
    session_factory: Callable[[], Session]
    engine: Engine
    export_dir: Path

    def session(self) -> Session:
        return self.session_factory()


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestContext]:
    db_path = tmp_path / "asst.db"
    export_dir = tmp_path / "exports"
    monkeypatch.setenv("ASST_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("ASST_TRAIL_KEY", TEST_TRAIL_KEY)
    monkeypatch.setenv("ASST_EXPORT_DIR", str(export_dir))
    monkeypatch.setenv("ASST_EXPORT_QUEUE", "inline")
    monkeypatch.setenv("ASST_LOG_RETENTION_DAYS", "180")
    monkeypatch.setenv("ASST_PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setenv("ASST_IAM_STUB_TOKENS", _stub_tokens())

    # 让 app.api.deps 的模块级 engine / iam 按本测试的环境变量重建
    import importlib

    from app import config as config_module
    from app.api import deps as deps_module
    from app.db import session as session_module

    importlib.reload(config_module)
    importlib.reload(session_module)
    importlib.reload(deps_module)

    from app.main import app as fastapi_app

    session_module.init_storage(deps_module.get_engine())
    engine = deps_module.get_engine()
    session_factory = session_module.build_session_factory(engine)

    with TestClient(fastapi_app) as client:
        yield TestContext(
            client=client,
            session_factory=session_factory,
            engine=engine,
            export_dir=export_dir,
        )

    engine.dispose()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_conversation(
    ctx: TestContext,
    *,
    token: str = EMPLOYEE_TOKEN,
    title: str = "虚构会话",
    site_id: uuid.UUID | None = SITE_A,
) -> str:
    response = ctx.client.post(
        "/api/assistant/conversations",
        headers=auth(token),
        json={"title": title, "site_id": str(site_id) if site_id else None},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def post_message(ctx: TestContext, conversation_id: str, content: str, *, token: str = EMPLOYEE_TOKEN):
    response = ctx.client.post(
        f"/api/assistant/conversations/{conversation_id}/messages",
        headers=auth(token),
        json={"content": content},
    )
    assert response.status_code == 201, response.text
    return response.json()


def post_answer(ctx: TestContext, conversation_id: str, content: str, *, token: str = EMPLOYEE_TOKEN):
    response = ctx.client.post(
        f"/api/assistant/conversations/{conversation_id}/answers",
        headers=auth(token),
        json={
            "content": content,
            "doc_refs": [{"doc_id": "doc-0001", "doc_version": "v3", "title": "虚构制度"}],
            "model_name": "stub-model-1",
            "prompt_version": "p-2026-01",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def post_tool_call(
    ctx: TestContext,
    conversation_id: str,
    *,
    tool_name: str = "crm.member.lookup",
    token: str = EMPLOYEE_TOKEN,
):
    response = ctx.client.post(
        f"/api/assistant/conversations/{conversation_id}/tool-calls",
        headers=auth(token),
        json={
            "tool_name": tool_name,
            "tool_version": "1.4.2",
            "params": {"phone": "13812345678", "email": "zhangsan@example.com"},
            "result_summary": "命中 1 条虚构会员记录，联系人 13812345678",
            "status": "ok",
            "confirmed_by": str(CONFIRMER_ID),
            "confirmed_at": "2026-01-02T03:04:05",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def post_blocked_event(ctx: TestContext, conversation_id: str, *, token: str = EMPLOYEE_TOKEN):
    response = ctx.client.post(
        f"/api/assistant/conversations/{conversation_id}/blocked-events",
        headers=auth(token),
        json={
            "block_reason": "kill_switch_on",
            "rule_id": "asst.kill_switch.global",
            "attempted_tool": "crm.member.export",
            "detail": "关停开关处于开启状态，拒绝导出",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def post_feedback(ctx: TestContext, conversation_id: str, rating: int, *, token: str = EMPLOYEE_TOKEN):
    response = ctx.client.post(
        f"/api/assistant/conversations/{conversation_id}/feedback",
        headers=auth(token),
        json={"rating": rating, "comment": "回答里有 13812345678 这样的号码"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def seed_full_conversation(ctx: TestContext, *, title: str = "虚构会话", token: str = EMPLOYEE_TOKEN) -> str:
    """建一个含 5 类留痕的会话：消息 / 回答 / 工具调用 / 拦截事件 / 反馈。"""
    conversation_id = create_conversation(ctx, token=token, title=title)
    post_message(ctx, conversation_id, "帮我看下会员 13812345678 的对接记录", token=token)
    post_answer(ctx, conversation_id, "按虚构制度 v3 第 4 条，该会员可对接。", token=token)
    post_tool_call(ctx, conversation_id, token=token)
    post_blocked_event(ctx, conversation_id, token=token)
    post_feedback(ctx, conversation_id, 2, token=token)
    return conversation_id