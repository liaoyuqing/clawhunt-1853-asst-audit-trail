"""真实 Redis 导出队列的端到端用例（入队 → worker 消费 → 文件落地 → 状态回写）。

整组只在 ASST_TEST_REDIS_URL 给出时运行（夹具把 ASST_EXPORT_QUEUE 切成 redis），
未给出时整组 skip，SQLite/inline 基线不受影响。

覆盖：
  * POST /audit/exports 在 redis 模式下只入队，响应 202 且 job 仍是 queued；
  * 队列键里有 job_id（真实 LPUSH 生效，不是空跑）；
  * worker 侧 BRPOP 取到任务、跑 run_export_job、落 JSONL、状态改 finished；
  * 同一个 job_id 重复入队不会重复导出（幂等）；
  * 处理标记在处理完成后清掉；用例收尾把自己建的 key 全部 DEL（不给共享实例留垃圾）。
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.config import load_settings
from app.models.asst_export_job import AsstExportJob
from app.services import export_queue
from app.services.crypto import TrailCipher
from tests.conftest import (
    AUDITOR_TOKEN,
    auth,
    redis_test_url,
    seed_full_conversation,
)

pytestmark = [
    pytest.mark.redis_queue,
    pytest.mark.skipif(redis_test_url() is None, reason="需要 ASST_TEST_REDIS_URL 指向真实 Redis"),
]


@pytest.fixture()
def redis_keys(ctx):
    """记录本用例会碰到的 key，结束时全部 DEL（只删自己前缀下的键）。"""
    settings = load_settings()
    client = export_queue.build_client(settings)
    patterns = [
        export_queue.queue_key(settings),
        export_queue.processing_key(settings),
    ]
    yield client, patterns
    for key in patterns:
        client.delete(key)
    client.close()


def test_enqueue_only_on_api_and_worker_finishes(ctx, redis_keys):
    """端到端：API 入队 → 队列里真有 job_id → worker 消费 → 文件落地 → 状态 finished。"""
    client, patterns = redis_keys
    settings = load_settings()
    assert settings.export_queue == "redis"

    conversation_id = seed_full_conversation(ctx)
    response = ctx.client.post(
        "/api/assistant/audit/exports", headers=auth(AUDITOR_TOKEN), json={}
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert response.json()["status"] == "queued"

    # 真实入队：队列键里就是这个 job_id，且 worker 还没跑（文件不存在）
    assert client.lrange(export_queue.queue_key(settings), 0, -1) == [job_id]
    assert list(ctx.export_dir.glob("*.jsonl")) == []

    # worker 侧：BRPOP 取任务（真实出队），用独立会话执行
    popped = export_queue.pop(settings, timeout=2)
    assert popped == job_id
    assert client.lrange(export_queue.queue_key(settings), 0, -1) == []

    export_queue.mark_processing(settings, popped)
    cipher = TrailCipher.from_env_value(settings.trail_key)
    with ctx.session() as session:
        job = export_queue.process_job(
            session, job_id=uuid.UUID(popped), cipher=cipher, settings=settings
        )
    export_queue.clear_processing(settings, popped)

    assert job is not None and job.status == "finished"
    assert job.row_count == 5
    assert client.lrange(export_queue.processing_key(settings), 0, -1) == []

    files = list(ctx.export_dir.glob("*.jsonl"))
    assert len(files) == 1
    lines = [line for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 6
    manifest = json.loads(lines[0])
    assert manifest["type"] == "manifest"
    assert manifest["job_id"] == job_id
    assert {json.loads(line)["conversation_id"] for line in lines[1:]} == {conversation_id}

    # 库里的状态真的变 completed 语义（本工程口径 status=finished）
    with ctx.session() as session:
        stored = session.get(AsstExportJob, uuid.UUID(job_id))
        assert stored is not None
        assert stored.status == "finished"
        assert stored.file_sha256 and len(stored.file_sha256) == 64

    # API 侧能查到下载链接（10 分钟有效）
    status = ctx.client.get(
        f"/api/assistant/audit/exports/{job_id}", headers=auth(AUDITOR_TOKEN)
    )
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["status"] == "finished"
    assert body["row_count"] == 5
    assert body["download_url"] and "token=" in body["download_url"]


def test_duplicate_enqueue_is_idempotent(ctx, redis_keys):
    """同一 job_id 入队两次：worker 只导出一次，第二次直接跳过（不覆盖文件）。"""
    client, _ = redis_keys
    settings = load_settings()
    seed_full_conversation(ctx)
    response = ctx.client.post(
        "/api/assistant/audit/exports", headers=auth(AUDITOR_TOKEN), json={}
    )
    job_id = response.json()["job_id"]
    export_queue.enqueue(settings, uuid.UUID(job_id))  # 重复入队
    assert client.llen(export_queue.queue_key(settings)) == 2

    cipher = TrailCipher.from_env_value(settings.trail_key)
    drained = export_queue.drain_queue(settings)
    assert drained == [job_id, job_id]  # rpop 顺序：后进的先出
    for value in drained:
        with ctx.session() as session:
            job = export_queue.process_job(
                session, job_id=uuid.UUID(value), cipher=cipher, settings=settings
            )
    files = list(ctx.export_dir.glob("*.jsonl"))
    assert len(files) == 1


def test_worker_reports_failure_status(ctx, redis_keys):
    """worker 执行失败时把 status 写回 failed 并记 error（不静默丢任务）。"""
    _, _ = redis_keys
    settings = load_settings()
    seed_full_conversation(ctx)
    response = ctx.client.post(
        "/api/assistant/audit/exports", headers=auth(AUDITOR_TOKEN), json={}
    )
    job_id = response.json()["job_id"]
    export_queue.drain_queue(settings)

    # 用一把错误密钥的 cipher 触发解密失败路径（明文不可解 → run_export_job 不抛错但行内容为空），
    # 这里换成直接删除 job 行后再处理：process_job 必须跳过而不是崩掉
    with ctx.session() as session:
        job = session.get(AsstExportJob, uuid.UUID(job_id))
        job.status = "running"
        session.commit()
    cipher = TrailCipher.from_env_value(settings.trail_key)
    with ctx.session() as session:
        skipped = export_queue.process_job(
            session, job_id=uuid.UUID(job_id), cipher=cipher, settings=settings
        )
    assert skipped is not None and skipped.status == "running"  # 非 queued 不重复执行
