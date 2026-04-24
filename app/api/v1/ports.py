"""
FFORS 港口 API 路由层
端口: /api/v1/ports/
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Port, get_async_session
from app.schemas.port import PortResponse

router = APIRouter(prefix="/ports", tags=["ports"])


async def get_db() -> AsyncSession:
    session_factory = get_async_session()
    async with session_factory() as session:
        yield session


@router.get(
    "/",
    response_model=list[PortResponse],
    summary="查询港口列表",
    description="支持按国家、区域过滤，用于前端下拉选择。",
)
async def list_ports(
    country: Optional[str] = Query(None, description="国家过滤"),
    region: Optional[str] = Query(None, description="区域过滤 (如 East Asia)"),
    q: Optional[str] = Query(None, description="搜索（代码/名称模糊匹配）"),
    db: AsyncSession = Depends(get_db),
) -> list[Port]:

    stmt = select(Port)
    if country:
        stmt = stmt.where(Port.country == country)
    if region:
        stmt = stmt.where(Port.region == region)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            Port.code.ilike(pattern)
            | Port.name_en.ilike(pattern)
            | Port.name_cn.ilike(pattern)
        )
    stmt = stmt.order_by(Port.code)

    result = await db.execute(stmt)
    return list(result.scalars().all())
