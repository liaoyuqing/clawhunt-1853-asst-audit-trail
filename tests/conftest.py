"""测试夹具：临时 SQLite（默认）或真实 PostgreSQL（每个用例独立 schema）+ FastAPI TestClient。

连接串只从环境变量读，仓库里没有任何主机 / 账号 / 口令：

  * ASST_TEST_DATABASE_URL  给出时整份测试改跑真实 PostgreSQL（驱动 psycopg3），
    每个用例在库里建一个独立 schema（asst_test_<uuid12>）并把 search_path 指过去，
    用例结束 DROP SCHEMA CASCADE —— 用例之间互不干扰，也不动库里的 public。
  * ASST_TEST_REDIS_URL     给出时导出队列的 Redis 用例才真正连队列，否则整组 skip。
  * 两者都不给时退化为临时文件 SQLite + inline 队列（离线可跑，见 README 验证边界）。

其余夹具与产品代码完全一致：应用装配、依赖注入、触发器安装都走真实代码路径。
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
from sqlalchemy import Engine, create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

TEST_TRAIL_KEY = "unit-test-only-key-0000000000000000000000000000"

# 真实中间件的连接串只从环境变量读：交付物与仓库里不出现任何主机、账号、口令
PG_TEST_URL_ENV = "ASST_TEST_DATABASE_URL"
REDIS_TEST_URL_ENV = "ASST_TEST_REDIS_URL"

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


def pg_test_url() -> str | None:
    """真实 PG 的连接串（环境变量给出时才有）。"""
    raw = (os.environ.get(PG_TEST_URL_ENV) or "").strip()
    return raw or None


def redis_test_url() -> str | None:
    """真实 Redis 的连接串（环境变量给出时才有）。"""
    raw = (os.environ.get(REDIS_TEST_URL_ENV) or "").strip()
    return raw or None


def _with_search_path(base_url: str, schema: str) -> str:
    """把连接串的 search_path 指到本用例的 schema（对 SQLAlchemy 与 psycopg 都生效）。"""
    url = make_url(base_url)
    query = dict(url.query)
    query["options"] = f"-c search_path={schema}"
    return url.set(query=query).render_as_string(hide_password=False)


@dataclass
class TestContext:
    client: TestClient
    session_factory: Callable[[], Session]
    engine: Engine
    export_dir: Path
    dialect: str

    def session(self) -> Session:
        return self.session_factory()


@pytest.fixture()
def ctx(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestContext]:
    pg_url = pg_test_url()
    admin_engine: Engine | None = None
    schema: str | None = None
    if pg_url:
        schema = "asst_test_" + uuid.uuid4().hex[:12]
        admin_engine = create_engine(pg_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        with admin_engine.connect() as conn:
            conn.execute(text('CREATE SCHEMA "' + schema + '"'))
        database_url = _with_search_path(pg_url, schema)
    else:
        database_url = f"sqlite+pysqlite:///{tmp_path / 'asst.db'}"

    export_dir = tmp_path / "exports"
    monkeypatch.setenv("ASST_DATABASE_URL", database_url)
    monkeypatch.setenv("ASST_TRAIL_KEY", TEST_TRAIL_KEY)
    monkeypatch.setenv("ASST_EXPORT_DIR", str(export_dir))
    # 标记了 redis_queue 的用例走真实 Redis 队列；其余用例保持 inline（不碰外部实例）
    wants_redis_queue = request.node.get_closest_marker("redis_queue") is not None
    if wants_redis_queue and redis_test_url():
        monkeypatch.setenv("ASST_EXPORT_QUEUE", "redis")
    else:
        monkeypatch.setenv("ASST_EXPORT_QUEUE", "inline")
    monkeypatch.setenv("ASST_LOG_RETENTION_DAYS", "180")
    monkeypatch.setenv("ASST_PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setenv("ASST_IAM_STUB_TOKENS", _stub_tokens())
    # Redis 用例需要显式给出；未给出时把地址清空，避免误连外部实例
    if redis_test_url():
        monkeypatch.setenv("ASST_REDIS_URL", redis_test_url())
    else:
        monkeypatch.delenv("ASST_REDIS_URL", raising=False)

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

    try:
        with TestClient(fastapi_app) as client:
            yield TestContext(
                client=client,
                session_factory=session_factory,
                engine=engine,
                export_dir=export_dir,
                dialect=engine.dialect.name,
            )
    finally:
        engine.dispose()
        if admin_engine is not None and schema is not None:
            with admin_engine.connect() as conn:
                conn.execute(text('DROP SCHEMA IF EXISTS "' + schema + '" CASCADE'))
            admin_engine.dispose()


# ---------- 方言无关的断言辅助（一律走 SQLAlchemy 列类型 / ORM，不手写方言函数） ----------


def count_rows(ctx: TestContext, model, *criteria) -> int:
    """按 ORM 表统计行数：布尔、UUID 列在两种方言上都按各自类型绑定。"""
    with ctx.engine.begin() as conn:
        stmt = select(func.count()).select_from(model.__table__)
        if criteria:
            stmt = stmt.where(*criteria)
        return int(conn.execute(stmt).scalar_one())


def tamper_trail_payload(ctx: TestContext, seq: int, *, value: str = "TAMPERED") -> None:
    """模拟「数据库超级用户绕过触发器」改一条历史留痕的 payload。

    先卸掉护栏，再整列写回 JSON（SQLite 写 JSON 文本、PostgreSQL 写 jsonb），
    不依赖 json_set 这类方言函数。
    """
    from app.models.asst_trail import AsstTrail
    from app.db.immutability import drop_immutability

    with ctx.engine.begin() as conn:
        drop_immutability(conn)
        payload = conn.execute(
            select(AsstTrail.__table__.c.payload).where(AsstTrail.__table__.c.seq == seq)
        ).scalar_one()
        mutated = dict(payload)
        mutated["content_redacted"] = value
        conn.execute(
            update(AsstTrail.__table__)
            .where(AsstTrail.__table__.c.seq == seq)
            .values(payload=mutated)
        )


def tamper_chain_payload_canonical(ctx: TestContext, seq: int, *, value: str = "{}") -> None:
    """改链记录本身（不改业务行）：先卸掉 asst_hash_chain 的 append-only 护栏。"""
    from app.models.asst_hash_chain import AsstHashChainEntry

    table = AsstHashChainEntry.__table__
    target = "asst_hash_chain"
    with ctx.engine.begin() as conn:
        if ctx.dialect == "postgresql":
            conn.execute(text(f"ALTER TABLE {target} DISABLE TRIGGER trg_{target}_append_only"))
        else:
            for suffix in ("no_update", "no_delete"):
                conn.execute(text(f"DROP TRIGGER IF EXISTS trg_{target}_{suffix}"))
        conn.execute(update(table).where(table.c.seq == seq).values(payload_canonical=value))


def delete_trail_row(ctx: TestContext, seq: int) -> None:
    """删一条留痕（不写清理标记）：先卸掉护栏。"""
    from app.models.asst_trail import AsstTrail
    from app.db.immutability import drop_immutability

    table = AsstTrail.__table__
    with ctx.engine.begin() as conn:
        drop_immutability(conn)
        conn.execute(table.delete().where(table.c.seq == seq))


def bulk_delete_trails(ctx: TestContext) -> None:
    """整表删除留痕（用于反证：窗口外一律被拒）。"""
    from app.models.asst_trail import AsstTrail

    with ctx.engine.begin() as conn:
        conn.execute(text("DELETE FROM asst_trail"))


def update_trail_ciphertext(ctx: TestContext, conversation_id: str) -> None:
    """直接改密文列（用于反证触发器拦得住）。"""
    from app.models.asst_trail import AsstTrail

    table = AsstTrail.__table__
    with ctx.engine.begin() as conn:
        conn.execute(
            update(table)
            .where(table.c.conversation_id == uuid.UUID(str(conversation_id)))
            .values(content_redacted="tampered")
        )


def set_hidden_by_user(ctx: TestContext, conversation_id: str, value) -> None:
    """白名单列更新（布尔值按方言正确绑定；传非布尔值用于反证类型校验）。"""
    from app.models.asst_trail import AsstTrail

    table = AsstTrail.__table__
    with ctx.engine.begin() as conn:
        conn.execute(
            update(table)
            .where(table.c.conversation_id == uuid.UUID(str(conversation_id)))
            .values(hidden_by_user=value)
        )


def set_hidden_by_user_raw(ctx: TestContext, conversation_id: str, value) -> None:
    """绕过 ORM 的布尔类型校验直接写 hidden_by_user（用于反证 DB 层也拒非布尔值）。

    SQLite 的 UUID 列存 32 位 hex，PostgreSQL 存 uuid：按方言给对应的绑定值。
    """
    cid = uuid.UUID(str(conversation_id))
    bound = cid.hex if ctx.dialect == "sqlite" else cid
    with ctx.engine.begin() as conn:
        conn.execute(
            text("UPDATE asst_trail SET hidden_by_user = :v WHERE conversation_id = :cid"),
            {"v": value, "cid": bound},
        )


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
