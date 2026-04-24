"""
FFORS 数据库种子数据加载脚本
使用方法：python -m scripts.seed_db
功能：
  1. 加载 data/seed_ports.json 中的港口数据
  2. 跳过已存在的记录（幂等操作）
"""

import asyncio
import json
import os
import sys

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.base import Base, get_async_engine
from app.models.port import Port
from app.utils.logger import get_logger

logger = get_logger("ffors.scripts.seed_db")


async def seed_ports():
    """加载港口种子数据，跳过已存在的记录。"""
    data_dir = os.environ.get("DATA_DIR", "data")
    seed_file = os.path.join(data_dir, "seed_ports.json")

    if not os.path.exists(seed_file):
        logger.error(f"种子文件不存在: {seed_file}")
        return

    with open(seed_file, "r", encoding="utf-8") as f:
        ports_data = json.load(f)

    engine = get_async_engine()

    # 创建所有表（如果不存在）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        # 获取已存在的港口代码
        result = await session.execute(select(Port.code))
        existing_codes = {row[0] for row in result.all()}

        new_count = 0
        skip_count = 0

        for port_data in ports_data:
            code = port_data.get("code")
            if code in existing_codes:
                skip_count += 1
                continue

            port = Port(**port_data)
            session.add(port)
            new_count += 1

        if new_count > 0:
            await session.commit()

        logger.info(
            f"港口种子数据加载完成: 新增 {new_count} 条, 跳过 {skip_count} 条 (已存在)"
        )


async def main():
    logger.info("=== FFORS 种子数据初始化 ===")
    await seed_ports()
    logger.info("=== 初始化完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
