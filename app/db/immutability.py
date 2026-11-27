"""只增不改的数据库层兜底：BEFORE UPDATE OR DELETE 触发器。

设计要点：
  * 白名单只有两列：hidden_by_user（布尔）与 purged_at（时间）；除这两列外任何列被改都直接
    RAISE，应用层即使写错 SQL 也改不动历史。
  * 物理删除只允许保留期清理任务在「清理窗口」内进行：asst_purge_guard 只有一行，清理任务
    在同一事务里打开窗口（写入 cutoff），触发器只放行 occurred_at < cutoff 的行；窗口关闭后
    DELETE 立刻恢复全拒。这比给清理任务一个特权连接更难被业务代码误用。
  * asst_hash_chain 与 audit_log 同样装触发器：链记录与审计日志都是只增不改。

两套方言各写一份 DDL（PostgreSQL 用 plpgsql + jsonb 差集，SQLite 用列清单 + RAISE），
列清单由 SQLAlchemy 元数据现算，不会与模型漂移。
"""
from __future__ import annotations

import logging

from datetime import datetime

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from app.models.asst_trail import AsstTrail

logger = logging.getLogger(__name__)

TRAIL_ERROR_CODE = "TRAIL_IMMUTABLE"
WHITELIST_COLUMNS = ("hidden_by_user", "purged_at")
APPEND_ONLY_TABLES = ("asst_hash_chain", "audit_log")

_PURGE_GUARD_DDL = """CREATE TABLE IF NOT EXISTS asst_purge_guard (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  active INTEGER NOT NULL DEFAULT 0,
  cutoff TIMESTAMP NULL
)"""


def _trail_columns_from_metadata() -> list[str]:
    """从 SQLAlchemy 元数据现算「不许改」的列清单，避免手写清单与模型漂移。"""
    return [c.name for c in AsstTrail.__table__.columns if c.name not in WHITELIST_COLUMNS]


def _sqlite_trail_ddl(forbidden: list[str]) -> list[str]:
    """三条 SQLite 触发器：禁改、布尔类型校验、禁删（清理窗口内放行）。"""
    cols = ", ".join(forbidden)
    no_update_msg = TRAIL_ERROR_CODE + ": asst_trail is append-only; only hidden_by_user/purged_at may change"
    bool_msg = TRAIL_ERROR_CODE + ": hidden_by_user must be a strict boolean"
    delete_msg = TRAIL_ERROR_CODE + ": physical delete is reserved for the retention purge window"
    return [
        "DROP TRIGGER IF EXISTS trg_asst_trail_no_update",
        "CREATE TRIGGER trg_asst_trail_no_update BEFORE UPDATE OF "
        + cols
        + " ON asst_trail BEGIN SELECT RAISE(ABORT, \""
        + no_update_msg
        + "\"); END",
        "DROP TRIGGER IF EXISTS trg_asst_trail_whitelist_type",
        "CREATE TRIGGER trg_asst_trail_whitelist_type BEFORE UPDATE OF hidden_by_user ON asst_trail "
        + "WHEN typeof(NEW.hidden_by_user) <> 'integer' OR NEW.hidden_by_user NOT IN (0, 1) "
        + "BEGIN SELECT RAISE(ABORT, \""
        + bool_msg
        + "\"); END",
        "DROP TRIGGER IF EXISTS trg_asst_trail_no_delete",
        "CREATE TRIGGER trg_asst_trail_no_delete BEFORE DELETE ON asst_trail "
        + "WHEN NOT (EXISTS (SELECT 1 FROM asst_purge_guard WHERE id = 1 AND active = 1) "
        + "AND OLD.occurred_at < (SELECT cutoff FROM asst_purge_guard WHERE id = 1)) "
        + "BEGIN SELECT RAISE(ABORT, \""
        + delete_msg
        + "\"); END",
    ]


def _sqlite_append_only_ddl(table: str, purgeable: bool = False) -> list[str]:
    """只增不改表：UPDATE 全拒；DELETE 全拒，purgeable 时清理窗口内放行。"""
    msg = TRAIL_ERROR_CODE + ": " + table + " is append-only"
    delete_when = ""
    if purgeable:
        delete_when = (
            "WHEN NOT (EXISTS (SELECT 1 FROM asst_purge_guard WHERE id = 1 AND active = 1) "
            "AND OLD.occurred_at < (SELECT cutoff FROM asst_purge_guard WHERE id = 1)) "
        )
    return [
        f"DROP TRIGGER IF EXISTS trg_{table}_no_update",
        f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
        + "BEGIN SELECT RAISE(ABORT, \""
        + msg
        + "\"); END",
        f"DROP TRIGGER IF EXISTS trg_{table}_no_delete",
        f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
        + delete_when
        + "BEGIN SELECT RAISE(ABORT, \""
        + msg
        + "\"); END",
    ]

_PG_TRAIL_FN = """CREATE OR REPLACE FUNCTION asst_trail_immutable_fn() RETURNS trigger AS $$
DECLARE
    old_payload jsonb;
    new_payload jsonb;
    guard_ok boolean;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        old_payload := to_jsonb(OLD) - 'hidden_by_user' - 'purged_at';
        new_payload := to_jsonb(NEW) - 'hidden_by_user' - 'purged_at';
        IF old_payload IS DISTINCT FROM new_payload THEN
            RAISE EXCEPTION 'TRAIL_IMMUTABLE: asst_trail is append-only; only hidden_by_user/purged_at may change'
                USING ERRCODE = 'P0001';
        END IF;
        IF jsonb_typeof(to_jsonb(NEW) -> 'hidden_by_user') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'TRAIL_IMMUTABLE: hidden_by_user must be a strict boolean'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    SELECT (g.active = 1 AND OLD.occurred_at < g.cutoff) INTO guard_ok
      FROM asst_purge_guard g WHERE g.id = 1;
    IF guard_ok IS TRUE THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'TRAIL_IMMUTABLE: physical delete is reserved for the retention purge window'
        USING ERRCODE = 'P0001';
END;
$$ LANGUAGE plpgsql"""


def _postgres_trail_ddl() -> list[str]:
    return [
        _PG_TRAIL_FN,
        "DROP TRIGGER IF EXISTS trg_asst_trail_immutable ON asst_trail",
        "CREATE TRIGGER trg_asst_trail_immutable BEFORE UPDATE OR DELETE ON asst_trail",
        "FOR EACH ROW EXECUTE FUNCTION asst_trail_immutable_fn()",
    ]


_PG_APPEND_ONLY_FN = """CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'TRAIL_IMMUTABLE: {table} is append-only' USING ERRCODE = 'P0001';
END;
$$ LANGUAGE plpgsql"""


_PG_PURGEABLE_APPEND_ONLY_FN = """CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $
DECLARE
    guard_ok boolean;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'TRAIL_IMMUTABLE: {table} is append-only' USING ERRCODE = 'P0001';
    END IF;
    SELECT (g.active = 1 AND OLD.occurred_at < g.cutoff) INTO guard_ok
      FROM asst_purge_guard g WHERE g.id = 1;
    IF guard_ok IS TRUE THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'TRAIL_IMMUTABLE: {table} is append-only' USING ERRCODE = 'P0001';
END;
$ LANGUAGE plpgsql"""


def _postgres_append_only_ddl(table: str, purgeable: bool = False) -> list[str]:
    fn = table + "_append_only_fn"
    if purgeable:
        body = _PG_PURGEABLE_APPEND_ONLY_FN.format(fn=fn, table=table)
    else:
        body = _PG_APPEND_ONLY_FN.format(fn=fn, table=table)
    return [
        body,
        f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}",
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table}",
        f"FOR EACH ROW EXECUTE FUNCTION {fn}()",
    ]


def install_immutability(engine: Engine) -> None:
    """建清理窗口表并装触发器（幂等）。

    SQLite 每次调用都重建触发器（DROP + CREATE），因此测试里「绕过 → 恢复」用同一个函数。
    """
    dialect = engine.dialect.name
    if dialect not in ("sqlite", "postgresql"):
        raise RuntimeError(f"不支持的方言 {dialect}：不可变触发器目前只提供 postgresql / sqlite 两套")

    with engine.begin() as conn:
        conn.execute(text(_PURGE_GUARD_DDL))
        # 守卫表固定一行；没有这一行时清理窗口更新不到，DELETE 永远被拒
        conn.execute(
            text(
                "INSERT INTO asst_purge_guard (id, active, cutoff) VALUES (1, 0, NULL) "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        if dialect == "sqlite":
            statements = _sqlite_trail_ddl(_trail_columns_from_metadata())
            for table in APPEND_ONLY_TABLES:
                statements += _sqlite_append_only_ddl(table, purgeable=table == "asst_hash_chain")
        else:
            statements = _postgres_trail_ddl()
            for table in APPEND_ONLY_TABLES:
                statements += _postgres_append_only_ddl(
                    table, purgeable=table == "asst_hash_chain"
                )
        for stmt in statements:
            conn.execute(text(stmt))
    logger.debug("immutability triggers installed on %s", dialect)


def drop_immutability(conn: Connection) -> None:
    """仅供测试模拟「数据库超级用户绕过」：卸掉 asst_trail 的护栏。"""
    if conn.engine.dialect.name == "postgresql":
        conn.execute(text("ALTER TABLE asst_trail DISABLE TRIGGER trg_asst_trail_immutable"))
        return
    for name in ("trg_asst_trail_no_update", "trg_asst_trail_whitelist_type", "trg_asst_trail_no_delete"):
        conn.execute(text("DROP TRIGGER IF EXISTS " + name))


def open_purge_window(conn: Connection, cutoff: datetime) -> None:
    """打开清理窗口：只放行 occurred_at < cutoff 的物理删除。"""
    conn.execute(
        text("UPDATE asst_purge_guard SET active = 1, cutoff = :cutoff WHERE id = 1"),
        {"cutoff": cutoff},
    )


def close_purge_window(conn: Connection) -> None:
    """关闭清理窗口：DELETE 立即恢复全拒。"""
    conn.execute(text("UPDATE asst_purge_guard SET active = 0, cutoff = NULL WHERE id = 1"))