"""
FFORS 数据库基类与连接工厂
提供 SQLAlchemy 异步引擎、Session 工厂及数据库初始化函数。
"""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""
    pass


# --- 异步引擎 (延迟初始化) ---
_engine = None
_async_session_factory = None


def get_async_engine():
    """获取异步数据库引擎（单例）。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=10,
            max_overflow=20,
        )
    return _engine


def get_async_session() -> async_sessionmaker[AsyncSession]:
    """获取异步 Session 工厂。"""
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_async_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


async def init_db():
    """初始化数据库：创建所有表（开发阶段使用，生产建议用 Alembic）。"""
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
