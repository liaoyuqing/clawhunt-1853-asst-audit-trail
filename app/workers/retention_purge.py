"""保留期清理定时任务入口：cron 每天跑一次。

用法：ASST_DATABASE_URL=... ASST_LOG_RETENTION_DAYS=180 python -m app.workers.retention_job
每次清理写一条审计日志（action=purge，含条数、时间范围、链尾哈希）。
"""
from __future__ import annotations

import json
import logging

from app.config import load_settings
from app.db.session import build_engine, build_session_factory, init_storage
from app.services.retention import purge_all_orgs

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    engine = build_engine(settings)
    init_storage(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        summaries = purge_all_orgs(session, retention_days=settings.retention_days)
        session.commit()
    for summary in summaries:
        print(json.dumps(summary.as_dict(), ensure_ascii=False))
    logger.info("retention purge done, %s org(s)", len(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
