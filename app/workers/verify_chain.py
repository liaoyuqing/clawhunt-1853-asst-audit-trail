"""链校验任务入口：cron 每天跑一次（或告警触发），发现断点即非零退出。

用法：ASST_DATABASE_URL=... python -m app.workers.verify_chain_job [YYYY-MM-DD]
      不带日期时校验「所有已建链的日期」。
退出码：0 全部完整；2 发现断点（断点位置打到 stdout，便于告警系统抓取）。
"""
from __future__ import annotations

import json
import logging
import sys

from sqlalchemy import select

from app.config import load_settings
from app.db.session import build_engine, build_session_factory, init_storage
from app.models.asst_hash_chain import AsstHashChainRun
from app.services.hash_chain import verify_chain

logger = logging.getLogger(__name__)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    engine = build_engine(settings)
    init_storage(engine)
    factory = build_session_factory(engine)
    broken = 0
    with factory() as session:
        if len(argv) > 1:
            from datetime import date

            target = date.fromisoformat(argv[1])
            org_ids = session.execute(select(AsstHashChainRun.org_id).distinct()).scalars().all()
            pairs = [(org_id, target) for org_id in org_ids]
        else:
            rows = session.execute(
                select(AsstHashChainRun.org_id, AsstHashChainRun.chain_date).distinct()
            ).all()
            pairs = [(row[0], row[1]) for row in rows]
        for org_id, chain_date in pairs:
            result = verify_chain(session, org_id=org_id, chain_date=chain_date)
            print(json.dumps(result.as_dict(), ensure_ascii=False))
            if not result.ok:
                broken += 1
        session.commit()
    return 0 if broken == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
