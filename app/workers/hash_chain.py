"""每日哈希链任务入口：cron 每天 02:10 跑一次，为前一日留痕补链并写审计日志。

用法：ASST_DATABASE_URL=... ASST_TRAIL_KEY=... python -m app.workers.hash_chain_job
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.config import load_settings
from app.db.session import build_engine, build_session_factory, init_storage
from app.models.asst_trail import AsstTrail
from app.services.hash_chain import daily_build

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    engine = build_engine(settings)
    init_storage(engine)
    factory = build_session_factory(engine)
    built = 0
    with factory() as session:
        org_ids = session.execute(select(AsstTrail.org_id).distinct()).scalars().all()
        for org_id in org_ids:
            results = daily_build(session, org_id)
            for result in results:
                logger.info(
                    "chain built org=%s date=%s rows=%s tail=%s",
                    result.org_id,
                    result.chain_date,
                    result.rows,
                    result.chain_tail_hash,
                )
                built += 1
        session.commit()
    logger.info("hash chain job done, %s chain(s) rebuilt", built)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
