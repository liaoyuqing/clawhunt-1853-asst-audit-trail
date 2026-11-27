"""初始结构：留痕、链、清理标记、导出任务、审计日志 + 只增不改触发器。

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-27

注意：本迁移在 PostgreSQL 15+ 上是权威建表路径（含 plpgsql 触发器）；
SQLite 分支用于本地与测试（列清单版触发器）。两套触发器与 app/db/immutability.py 一致。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.db.immutability import (
    _PG_PURGEABLE_APPEND_ONLY_FN,
    _PG_APPEND_ONLY_FN,
    _PG_TRAIL_FN,
    _PURGE_GUARD_DDL,
    _sqlite_append_only_ddl,
    _sqlite_trail_ddl,
    _trail_columns_from_metadata,
)

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asst_conversation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("hidden_by_user", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_asst_conversation_org_id", "asst_conversation", ["org_id"])
    op.create_index("ix_asst_conversation_site_id", "asst_conversation", ["site_id"])
    op.create_index(
        "ix_asst_conversation_org_user", "asst_conversation", ["org_id", "user_id", "created_at"]
    )

    op.create_table(
        "asst_trail",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("chain_date", sa.Date(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("related_trail_id", sa.Uuid(), nullable=True),
        sa.Column("content_enc", sa.Text(), nullable=True),
        sa.Column("content_redacted", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("doc_refs", sa.JSON(), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        sa.Column("tool_name", sa.String(128), nullable=True),
        sa.Column("tool_version", sa.String(64), nullable=True),
        sa.Column("params_redacted", sa.JSON(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("block_reason", sa.String(64), nullable=True),
        sa.Column("rule_id", sa.String(64), nullable=True),
        sa.Column("rating", sa.SmallInteger(), nullable=True),
        sa.Column("comment_redacted", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("hidden_by_user", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("purged_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("org_id", "seq", name="uq_asst_trail_org_seq"),
    )
    for index_name, columns in (
        ("ix_asst_trail_org_id", ["org_id"]),
        ("ix_asst_trail_site_id", ["site_id"]),
        ("ix_asst_trail_conversation_id", ["conversation_id"]),
        ("ix_asst_trail_user_id", ["user_id"]),
        ("ix_asst_trail_chain_date", ["chain_date"]),
        ("ix_asst_trail_occurred_at", ["occurred_at"]),
        ("ix_asst_trail_kind", ["kind"]),
        ("ix_asst_trail_tool_name", ["tool_name"]),
        ("ix_asst_trail_org_chain_date_seq", ["org_id", "chain_date", "seq"]),
        ("ix_asst_trail_conv_occurred", ["conversation_id", "occurred_at"]),
        ("ix_asst_trail_org_user_occurred", ["org_id", "user_id", "occurred_at"]),
    ):
        op.create_index(index_name, "asst_trail", columns)

    op.create_table(
        "asst_trail_counter",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("name", sa.String(32), nullable=False, server_default="trail"),
        sa.Column("last_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "asst_hash_chain",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("chain_date", sa.Date(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("trail_id", sa.Uuid(), nullable=False),
        sa.Column("trail_seq", sa.BigInteger(), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("row_hash", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_canonical", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "chain_date", "seq", name="uq_asst_hash_chain_org_date_seq"),
        sa.UniqueConstraint(
            "org_id", "chain_date", "trail_id", name="uq_asst_hash_chain_org_date_trail"
        ),
    )
    op.create_index("ix_asst_hash_chain_date_org", "asst_hash_chain", ["chain_date", "org_id"])
    op.create_index("ix_asst_hash_chain_occurred_at", "asst_hash_chain", ["occurred_at"])

    op.create_table(
        "asst_hash_chain_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("chain_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="ok"),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chain_head", sa.String(64), nullable=True),
        sa.Column("chain_tail_hash", sa.String(64), nullable=True),
        sa.Column("first_break_seq", sa.Integer(), nullable=True),
        sa.Column("break_reason", sa.String(64), nullable=True),
        sa.Column("expected_hash", sa.String(64), nullable=True),
        sa.Column("actual_hash", sa.String(64), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("org_id", "chain_date", name="uq_asst_hash_chain_run_org_date"),
    )

    op.create_table(
        "asst_purge_marker",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("chain_date", sa.Date(), nullable=False),
        sa.Column("purged_from_seq", sa.BigInteger(), nullable=False),
        sa.Column("purged_to_seq", sa.BigInteger(), nullable=False),
        sa.Column("chain_seq_from", sa.Integer(), nullable=True),
        sa.Column("chain_seq_to", sa.Integer(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("purged_at", sa.DateTime(), nullable=False),
        sa.Column("last_purged_row_hash", sa.String(64), nullable=True),
        sa.Column("audit_log_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_asst_purge_marker_org_range", "asst_purge_marker", ["org_id", "purged_from_seq", "purged_to_seq"]
    )
    op.create_index("ix_asst_purge_marker_org_date", "asst_purge_marker", ["org_id", "chain_date"])
    op.create_index("ix_asst_purge_marker_chain_date", "asst_purge_marker", ["chain_date"])
    op.create_index("ix_asst_purge_marker_org_id", "asst_purge_marker", ["org_id"])

    op.create_table(
        "asst_export_job",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("format", sa.String(16), nullable=False, server_default="jsonl"),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("file_sha256", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_asst_export_job_org_created", "asst_export_job", ["org_id", "created_at"]
    )
    op.create_index("ix_asst_export_job_org_id", "asst_export_job", ["org_id"])
    op.create_index("ix_asst_export_job_site_id", "asst_export_job", ["site_id"])

    op.create_table(
        "asst_export_token",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_asst_export_token_job_id", "asst_export_token", ["job_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("operator_id", sa.Uuid(), nullable=True),
        sa.Column("operator_role", sa.String(64), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
    )
    op.create_index("ix_audit_log_org_action_created", "audit_log", ["org_id", "action", "created_at"])
    op.create_index("ix_audit_log_org_id", "audit_log", ["org_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_target_id", "audit_log", ["target_id"])

    _install_triggers()


def _install_triggers() -> None:
    """装只增不改触发器（PostgreSQL 用 plpgsql，SQLite 用列清单版）。"""
    bind = op.get_bind()
    bind.exec_driver_sql(_PURGE_GUARD_DDL)
    bind.exec_driver_sql(
        "INSERT INTO asst_purge_guard (id, active, cutoff) VALUES (1, 0, NULL) "
        "ON CONFLICT (id) DO NOTHING"
    )
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(_PG_TRAIL_FN)
        bind.exec_driver_sql(
            "CREATE TRIGGER trg_asst_trail_immutable BEFORE UPDATE OR DELETE ON asst_trail "
            "FOR EACH ROW EXECUTE FUNCTION asst_trail_immutable_fn()"
        )
        for table, purgeable in (("asst_hash_chain", True), ("audit_log", False)):
            fn = f"{table}_append_only_fn"
            template = _PG_PURGEABLE_APPEND_ONLY_FN if purgeable else _PG_APPEND_ONLY_FN
            bind.exec_driver_sql(template.format(fn=fn, table=table))
            bind.exec_driver_sql(
                f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {fn}()"
            )
        return
    for stmt in _sqlite_trail_ddl(_trail_columns_from_metadata()):
        bind.exec_driver_sql(stmt)
    for table, purgeable in (("asst_hash_chain", True), ("audit_log", False)):
        for stmt in _sqlite_append_only_ddl(table, purgeable=purgeable):
            bind.exec_driver_sql(stmt)


def downgrade() -> None:
    """回滚：先卸触发器再删表（顺序反了会被自己的护栏挡住）。"""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name, table in (
            ("trg_asst_trail_immutable", "asst_trail"),
            ("trg_asst_hash_chain_append_only", "asst_hash_chain"),
            ("trg_audit_log_append_only", "audit_log"),
        ):
            bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    else:
        for table in ("asst_trail", "asst_hash_chain", "audit_log"):
            for suffix in ("no_update", "no_delete", "whitelist_type"):
                bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS trg_{table}_{suffix}")

    op.drop_table("audit_log")
    op.drop_table("asst_export_token")
    op.drop_table("asst_export_job")
    op.drop_table("asst_purge_marker")
    op.drop_table("asst_hash_chain_run")
    op.drop_table("asst_hash_chain")
    op.drop_table("asst_trail_counter")
    op.drop_table("asst_trail")
    op.drop_table("asst_conversation")
    bind.exec_driver_sql("DROP TABLE IF EXISTS asst_purge_guard")
