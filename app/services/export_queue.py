"""Redis 导出队列：跨进程 worker 的真实队列实现。

队列用裸 Redis list（不引入 RQ 之类的额外依赖，生产可用同一协议换 RQ/Celery）：
  * 入队  LPUSH  '{prefix}export:queue' 一个 job_id；
  * 出队  worker 侧 BRPOP 阻塞取任务 → 开自己的 DB 会话跑 run_export_job
          → 成功改 status=finished、失败改 status=failed 并写 error；
  * 处理标记  worker 开始处理时把 job 置 running，进程被杀也能看到卡住的任务；
  * 幂等  同一 job_id 重复入队时只在 status=queued 才执行，避免重复导出。

连接串只从 ASST_REDIS_URL 读（见 app/config.py），代码里没有任何主机/口令默认值，
本模块也不把连接串写进日志（异常文案只到类型与 job_id，不含凭据）。
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.config import Settings, load_settings
from app.models.asst_export_job import AsstExportJob
from app.services.crypto import TrailCipher
from app.services.export import run_export_job

logger = logging.getLogger(__name__)

QUEUE_SUFFIX = "export:queue"
PROCESSING_SUFFIX = "export:processing"


def queue_key(settings: Settings) -> str:
    """队列键：前缀可配，便于与其它业务隔离与清理。"""
    return f"{settings.redis_key_prefix}{QUEUE_SUFFIX}"


def processing_key(settings: Settings) -> str:
    """在处理集合：worker 取到但还没执行完的任务。"""
    return f"{settings.redis_key_prefix}{PROCESSING_SUFFIX}"


def build_client(settings: Settings):
    """建 Redis 客户端（decode_responses=True，键值都是字符串）。"""
    if not settings.redis_url:
        raise RuntimeError("缺少 ASST_REDIS_URL，无法连接导出队列")
    import redis  # 延迟导入：不接 Redis 时（inline/thread）不要求装 redis 包

    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
    )


def enqueue(settings: Settings, job_id: uuid.UUID) -> int:
    """把任务推进队列，返回当前队列长度（出队由 app/workers/export_worker.py 做）。"""
    client = build_client(settings)
    try:
        depth = int(client.lpush(queue_key(settings), str(job_id)))
    finally:
        client.close()
    logger.info("enqueued export job %s, queue depth=%s", job_id, depth)
    return depth


def pop(settings: Settings, timeout: int | None = None) -> str | None:
    """阻塞取一个 job_id（BRPOP；超时返回 None）。"""
    client = build_client(settings)
    try:
        result = client.brpop(
            queue_key(settings), timeout=timeout or settings.redis_brpop_timeout
        )
    finally:
        client.close()
    if not result:
        return None
    return result[1]


def mark_processing(settings: Settings, job_id: str) -> None:
    client = build_client(settings)
    try:
        client.lpush(processing_key(settings), job_id)
    finally:
        client.close()


def clear_processing(settings: Settings, job_id: str) -> None:
    client = build_client(settings)
    try:
        client.lrem(processing_key(settings), 0, job_id)
    finally:
        client.close()


def drain_queue(settings: Settings) -> list[str]:
    """把队列里剩余的任务全部取出（测试与停机排空用）。"""
    client = build_client(settings)
    drained: list[str] = []
    try:
        while True:
            result = client.rpop(queue_key(settings))
            if not result:
                break
            drained.append(result)
    finally:
        client.close()
    return drained


def process_job(
    session: Session, *, job_id: uuid.UUID, cipher: TrailCipher, settings: Settings
) -> AsstExportJob | None:
    """执行队里取到的一个任务：跳过非 queued 的（幂等），失败落 status=failed。"""
    job = session.get(AsstExportJob, job_id)
    if job is None:
        logger.warning("export job %s not found, skip", job_id)
        return None
    if job.status != "queued":
        logger.info("export job %s status=%s, skip (idempotent)", job_id, job.status)
        return job
    try:
        job = run_export_job(session, job_id=job_id, cipher=cipher, settings=settings)
        session.commit()
    except Exception as exc:  # noqa: BLE001 - worker 必须把失败落库而不是静默
        session.rollback()
        failed = session.get(AsstExportJob, job_id)
        if failed is not None:
            failed.status = "failed"
            failed.error = f"{type(exc).__name__}: {exc}"
            session.commit()
        logger.exception("export job %s failed", job_id)
        return failed
    logger.info("export job %s finished, rows=%s", job_id, job.row_count)
    return job
