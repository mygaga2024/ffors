"""
FFORS 供应商 API 路由层
端口: /api/v1/vendors/
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Vendor, get_async_session
from app.schemas.vendor import VendorCreate, VendorUpdate, VendorResponse
from app.utils.logger import get_logger

logger = get_logger("ffors.api.vendors")
router = APIRouter(prefix="/vendors", tags=["vendors"])


async def get_db() -> AsyncSession:
    session_factory = get_async_session()
    async with session_factory() as session:
        yield session


# ─────────────────────────────────────────────
# GET /api/v1/vendors/ — 供应商列表
# ─────────────────────────────────────────────

@router.get(
    "/",
    response_model=list[VendorResponse],
    summary="查询供应商列表",
)
async def list_vendors(
    vendor_type: Optional[str] = Query(None, description="类型过滤: carrier / forwarder"),
    is_active: Optional[bool] = Query(None, description="是否启用"),
    db: AsyncSession = Depends(get_db),
) -> list[Vendor]:

    stmt = select(Vendor)
    if vendor_type:
        stmt = stmt.where(Vendor.vendor_type == vendor_type)
    if is_active is not None:
        stmt = stmt.where(Vendor.is_active == is_active)
    stmt = stmt.order_by(Vendor.name)

    result = await db.execute(stmt)
    return list(result.scalars().all())


# ─────────────────────────────────────────────
# POST /api/v1/vendors/ — 创建供应商
# ─────────────────────────────────────────────

@router.post(
    "/",
    response_model=VendorResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建供应商",
)
async def create_vendor(
    data: VendorCreate,
    db: AsyncSession = Depends(get_db),
) -> Vendor:

    # 检查名称是否已存在
    existing = await db.execute(
        select(Vendor).where(Vendor.name == data.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"供应商 '{data.name}' 已存在",
        )

    vendor = Vendor(**data.model_dump())
    db.add(vendor)
    await db.commit()
    await db.refresh(vendor)
    logger.info(f"创建供应商: {vendor.name} ({vendor.vendor_type})")
    return vendor


# ─────────────────────────────────────────────
# GET /api/v1/vendors/{id} — 查询单个供应商
# ─────────────────────────────────────────────

@router.get(
    "/{vendor_id}",
    response_model=VendorResponse,
    summary="查询供应商详情",
)
async def get_vendor(
    vendor_id: int,
    db: AsyncSession = Depends(get_db),
) -> Vendor:

    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"供应商 ID {vendor_id} 不存在",
        )
    return vendor


# ─────────────────────────────────────────────
# PATCH /api/v1/vendors/{id} — 更新供应商
# ─────────────────────────────────────────────

@router.patch(
    "/{vendor_id}",
    response_model=VendorResponse,
    summary="更新供应商信息",
)
async def update_vendor(
    vendor_id: int,
    data: VendorUpdate,
    db: AsyncSession = Depends(get_db),
) -> Vendor:

    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"供应商 ID {vendor_id} 不存在",
        )

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(vendor, field, value)

    await db.commit()
    await db.refresh(vendor)
    logger.info(f"更新供应商: ID={vendor_id}, 字段={list(update_data.keys())}")
    return vendor
