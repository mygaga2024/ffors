"""
Alembic 迁移环境配置
从 app.config 读取 DATABASE_URL，自动检测所有 ORM 模型。
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# --- 导入 FFORS 模型，确保 Alembic 能检测到所有表 ---
from app.models import Base  # noqa: F401
from app.config import settings

# Alembic Config object
config = context.config

# 从 app.config 注入数据库 URL（覆盖 alembic.ini 中的占位符）
config.set_main_option("sqlalchemy.url", settings.database_url.replace(
    "postgresql+asyncpg", "postgresql"
))

# 配置日志
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标元数据（用于 autogenerate）
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """以离线模式运行迁移（只生成 SQL 不执行）。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """以在线模式运行迁移（连接数据库并执行）。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
