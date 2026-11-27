"""Alembic 环境：连接串从 ASST_DATABASE_URL 读，元数据取 app.models。"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import load_settings
from app.db.base import Base
import app.models  # noqa: F401 - 让全部模型注册进元数据

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# configparser 会把 % 当插值符：连接串里出现百分号转义（例如口令中的 # 写成 %23、
# 或 query 里带 options=...%20search_path...）时，必须先转义成 %% 再写入。
# 读取侧（get_section）会还原成原值，所以最终连到的仍是同一条串。
config.set_main_option(
    "sqlalchemy.url", load_settings().sqlalchemy_url.replace("%", "%%")
)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
