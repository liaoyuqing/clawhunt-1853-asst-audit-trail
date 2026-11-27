"""导出：JSONL 异步导出、10 分钟有效下载链接、单次可用、内容可复核。

覆盖：202 受理 → 状态查询 → 下载 → 重复下载 410 → 过期 410 → 签名篡改 403 →
JSONL 每行含 seq/row_hash 可离线复核、导出范围与筛选一致、导出本身写审计日志。
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.db.base import utcnow
from app.services.audit_log import ACTION_EXPORT_DOWNLOAD, query_audit_log
from tests.conftest import (
    AUDITOR_TOKEN,
    EMPLOYEE_TOKEN,
    ORG_ID,
    auth,
    create_conversation,
    post_answer,
    post_message,
    seed_full_conversation,
)


def _create_export(ctx, payload: dict | None = None) -> str:
    response = ctx.client.post(
        "/api/assistant/audit/exports",
        headers=auth(AUDITOR_TOKEN),
        json=payload or {},
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def test_export_returns_202_with_job_id(ctx):
    """导出受理返回 202 + job_id（异步语义）。"""
    seed_full_conversation(ctx)
    response = ctx.client.post(
        "/api/assistant/audit/exports", headers=auth(AUDITOR_TOKEN), json={}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert uuid.UUID(body["job_id"])
    assert body["status"] in {"queued", "running", "finished"}


def test_export_status_offers_download_link(ctx):
    """任务完成 → 返回 download_url 与 10 分钟有效期。"""
    seed_full_conversation(ctx)
    job_id = _create_export(ctx)
    status = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(AUDITOR_TOKEN)
    )
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["status"] == "finished"
    assert body["format"] == "jsonl"
    assert body["row_count"] == 5
    assert body["download_url"].startswith("http://testserver/api/assistant/audit/exports/")
    assert "token=" in body["download_url"] and "sig=" in body["download_url"]
    assert body["expires_at"] is not None


def test_download_streams_jsonl(ctx):
    """下载内容是真 JSONL：首行 manifest，其余每行一条留痕，可逐行解析。"""
    conversation_id = seed_full_conversation(ctx)
    job_id = _create_export(ctx)
    url = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(AUDITOR_TOKEN)
    ).json()["download_url"]
    path = url.split("http://testserver", 1)[1]
    response = ctx.client.get(path)
    assert response.status_code == 200, response.text
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) == 6
    manifest = json.loads(lines[0])
    assert manifest["type"] == "manifest"
    assert manifest["format"] == "jsonl"
    records = [json.loads(line) for line in lines[1:]]
    assert all(record["type"] == "trail" for record in records)
    assert all("seq" in record and "content_sha256" in record for record in records)
    assert {record["conversation_id"] for record in records} == {conversation_id}
    message = [r for r in records if r["kind"] == "message"][0]
    assert "13812345678" in message["content"]
    assert "13812345678" not in message["content_redacted"]


def test_download_single_use(ctx):
    """令牌单次可用：第二次下载 410。"""
    seed_full_conversation(ctx)
    job_id = _create_export(ctx)
    url = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(AUDITOR_TOKEN)
    ).json()["download_url"]
    path = url.split("http://testserver", 1)[1]
    assert ctx.client.get(path).status_code == 200
    second = ctx.client.get(path)
    assert second.status_code == 410
    assert second.json()["detail"]["code"] == "DOWNLOAD_EXPIRED"


def test_download_expires_after_ten_minutes(ctx):
    """把令牌有效期改到过期时刻 → 下载 410（10 分钟边界的可测形式）。"""
    seed_full_conversation(ctx)
    job_id = _create_export(ctx)
    url = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(AUDITOR_TOKEN)
    ).json()["download_url"]
    with ctx.engine.begin() as conn:
        conn.execute(
            text("UPDATE asst_export_token SET expires_at = :past"),
            {"past": utcnow() - timedelta(minutes=1)},
        )
    path = url.split("http://testserver", 1)[1]
    response = ctx.client.get(path)
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "DOWNLOAD_EXPIRED"


def test_download_rejects_tampered_signature(ctx):
    seed_full_conversation(ctx)
    job_id = _create_export(ctx)
    url = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(AUDITOR_TOKEN)
    ).json()["download_url"]
    path = url.split("http://testserver", 1)[1]
    tampered = path[:-4] + "dead"
    response = ctx.client.get(tampered)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "DOWNLOAD_FORBIDDEN"


def test_export_honours_filters(ctx):
    """导出复现同一套筛选：blocked=true 只导出被拦截的会话。"""
    blocked_conv = seed_full_conversation(ctx, title="带拦截")
    quiet = create_conversation(ctx, title="无拦截")
    post_message(ctx, quiet, "普通消息")
    post_answer(ctx, quiet, "普通回答")
    job_id = _create_export(ctx, {"blocked": True})
    status = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(AUDITOR_TOKEN)
    ).json()
    assert status["row_count"] == 5
    url = status["download_url"]
    response = ctx.client.get(url.split("http://testserver", 1)[1])
    records = [json.loads(line) for line in response.text.splitlines()[1:] if line.strip()]
    assert {r["conversation_id"] for r in records} == {blocked_conv}


def test_export_file_has_sha256(ctx):
    """导出文件指纹落库，便于事后核对文件没被换过。"""
    seed_full_conversation(ctx)
    job_id = _create_export(ctx)
    status = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(AUDITOR_TOKEN)
    ).json()
    assert status["file_sha256"] and len(status["file_sha256"]) == 64
    files = list(ctx.export_dir.glob("*.jsonl"))
    assert len(files) == 1
    import hashlib

    digest = hashlib.sha256()
    for line in files[0].read_text(encoding="utf-8").splitlines(keepends=True):
        digest.update(line.encode("utf-8"))
    assert digest.hexdigest() == status["file_sha256"]


def test_download_writes_audit_record(ctx):
    seed_full_conversation(ctx)
    job_id = _create_export(ctx)
    url = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(AUDITOR_TOKEN)
    ).json()["download_url"]
    ctx.client.get(url.split("http://testserver", 1)[1])
    with ctx.session() as session:
        entries = query_audit_log(session, ORG_ID, action=ACTION_EXPORT_DOWNLOAD)
    assert len(entries) == 1
    assert entries[0].target_id == job_id


def test_export_job_isolated_per_org(ctx):
    """其他 org 的审计角色查不到本 org 的导出任务 → 404。"""
    from tests.conftest import OTHER_ORG_AUDITOR_TOKEN

    seed_full_conversation(ctx)
    job_id = _create_export(ctx)
    response = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(OTHER_ORG_AUDITOR_TOKEN)
    )
    assert response.status_code == 404


def test_exports_are_separate_files(ctx):
    """两个任务各自落一个文件，互不覆盖。"""
    seed_full_conversation(ctx)
    first = _create_export(ctx)
    second = _create_export(ctx)
    assert first != second
    files = sorted(path.name for path in ctx.export_dir.glob("*.jsonl"))
    assert len(files) == 2
    assert any(first in name for name in files)
    assert any(second in name for name in files)

def test_jsonl_has_row_hash_after_chain_build(ctx):
    """提案 ⑤：JSONL 每行含 row_hash，可离线逐行复核（未建链时为 null）。"""
    from datetime import date

    from app.services.hash_chain import build_chain
    from tests.conftest import ORG_ID

    seed_full_conversation(ctx)
    with ctx.session() as session:
        build_chain(session, org_id=ORG_ID, chain_date=date.today())
        session.commit()

    job_id = _create_export(ctx)
    url = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(AUDITOR_TOKEN)
    ).json()["download_url"]
    response = ctx.client.get(url.split("http://testserver", 1)[1])
    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.strip()]
    manifest = json.loads(lines[0])
    assert "row_hash_note" in manifest
    records = [json.loads(line) for line in lines[1:]]
    assert all(record["row_hash"] for record in records)
    assert all(len(record["row_hash"]) == 64 for record in records)
    assert len({record["row_hash"] for record in records}) == len(records)

    with ctx.session() as session:
        rows = session.execute(
            text("SELECT trail_id, row_hash FROM asst_hash_chain WHERE org_id = :org"),
            {"org": ORG_ID.hex},
        ).all()
    # SQLite 里 UUID 以 32 位 hex 存储，比对前统一成带连字符的规范形式
    by_trail = {str(uuid.UUID(str(trail_id))): row_hash for trail_id, row_hash in rows}
    assert len(by_trail) == len(records)
    for record in records:
        assert by_trail[record["trail_id"]] == record["row_hash"]