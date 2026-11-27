"""FastAPI 应用装配。

启动：ASST_TRAIL_KEY=... ASST_DATABASE_URL=... uvicorn app.main:app
本模块在 import 时读环境变量，因此配置变更后需重启进程（ulid 常驻服务，符合预期）。
"""
from __future__ import annotations

from fastapi import FastAPI

from app.api.assistant import audit, conversations, ops
from app.api.deps import get_engine
from app.db.session import init_storage

app = FastAPI(
    title="会员空间平台 · 员工 AI 助手对话留痕",
    description=(
        "对话留痕（只增不改）、每日哈希链、保留期清理与审计查询/导出接口。"
        "审计接口仅审计角色可用，每次查看与导出本身也写审计日志。"
    ),
    version="1.0.0",
)

app.include_router(audit.router)
app.include_router(conversations.router)
app.include_router(ops.router)


@app.on_event("startup")
def _startup() -> None:
    """启动时建表 + 装触发器（幂等）；生产由 Alembic 负责建表。"""
    init_storage(get_engine())


@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}
