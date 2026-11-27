"""Redis 导出队列的 worker 入口（独立进程，可与 API 分机部署）。

用法：
  ASST_EXPORT_QUEUE=redis ASST_REDIS_URL=redis://:PASSWORD@HOST:6379/5 \
  ASST_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME \
  ASST_TRAIL_KEY=KEY python -m app.workers.export_worker --max-jobs 0

参数：
  --max-jobs N   处理 N 个任务后退出（0 = 常驻；测试与一次性验证用 1）
  --once         不阻塞，排空当前队列后退出
队列键：ASST_REDIS_KEY_PREFIX + "export:queue"（默认 asst1853:export:queue）。
"""
from __future__ import annotations

import argparse
import logging
import uuid

from app.config import load_settings
from app.db.session import build_engine, build_session_factory
from app.services import export_queue
from app.services.crypto import TrailCipher

logger = logging.getLogger(__name__)


def run(max_jobs: int = 0, once: bool = False) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    if settings.export_queue != "redis":
        raise SystemExit("ASST_EXPORT_QUEUE 必须是 redis 才能跑本 worker")
    cipher = TrailCipher.from_env_value(settings.trail_key)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    handled = 0
    try:
        while max_jobs <= 0 or handled < max_jobs:
            job_id = export_queue.pop(settings, timeout=1 if once else None)
            if job_id is None:
                if once:
                    break
                continue
            export_queue.mark_processing(settings, job_id)
            try:
                with factory() as session:
                    job = export_queue.process_job(
                        session, job_id=uuid.UUID(job_id), cipher=cipher, settings=settings
                    )
                handled += 1
                logger.info("handled job %s (status=%s)", job_id, getattr(job, "status", None))
            finally:
                export_queue.clear_processing(settings, job_id)
    finally:
        engine.dispose()
    logger.info("export worker exit, handled=%s", handled)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Redis 导出队列 worker")
    parser.add_argument("--max-jobs", type=int, default=0, help="处理多少个任务后退出（0=常驻）")
    parser.add_argument("--once", action="store_true", help="排空当前队列后退出，不阻塞等待")
    args = parser.parse_args()
    return run(max_jobs=args.max_jobs, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
