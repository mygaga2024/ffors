"""
FFORS 报价 API 路由层 (Gateway)
端口: /api/v1/rates/
契约锁定：路由路径与字段名称一经确认，未经允许不得修改 (DEVELOPMENT_PROTOCOL.md §4)
"""

from typing import Optional
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, status
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OceanRate, get_async_session
from app.schemas.rate import RateCreate, RateResponse, RateBatchImportResult
from app.utils.logger import get_logger

logger = get_logger("ffors.api.rates")
router = APIRouter(prefix="/rates", tags=["rates"])


# ─────────────────────────────────────────────
# 依赖注入：数据库 Session
# ─────────────────────────────────────────────

async def get_db() -> AsyncSession:
    session_factory = get_async_session()
    async with session_factory() as session:
        yield session


# ─────────────────────────────────────────────
# GET /api/v1/rates/ — 查询报价列表
# ─────────────────────────────────────────────

@router.get(
    "/",
    response_model=list[RateResponse],
    summary="查询报价列表",
    description="支持按起运港、目的港、船公司、日期范围过滤，默认返回最新 100 条。",
)
async def list_rates(
    pol_code: Optional[str] = Query(None, description="起运港代码，如 CNSHA"),
    pod_code: Optional[str] = Query(None, description="目的港代码，如 NLRTM"),
    carrier: Optional[str] = Query(None, description="船公司名称（模糊匹配）"),
    valid_after: Optional[date] = Query(None, description="报价有效期下限"),
    limit: int = Query(100, ge=1, le=500, description="返回条数上限"),
    offset: int = Query(0, ge=0, description="分页偏移量"),
    db: AsyncSession = Depends(get_db),
) -> list[OceanRate]:

    conditions = []
    if pol_code:
        conditions.append(OceanRate.pol_code == pol_code.strip().upper())
    if pod_code:
        conditions.append(OceanRate.pod_code == pod_code.strip().upper())
    if carrier:
        conditions.append(OceanRate.carrier.ilike(f"%{carrier.strip()}%"))
    if valid_after:
        conditions.append(OceanRate.valid_to >= valid_after)

    stmt = (
        select(OceanRate)
        .where(and_(*conditions) if conditions else True)
        .order_by(OceanRate.created_at.desc())
        .offset(offset)
        .limit(limit)
    )

    result = await db.execute(stmt)
    rates = result.scalars().all()
    logger.info(f"查询报价列表: pol={pol_code}, pod={pod_code}, 返回 {len(rates)} 条")
    return list(rates)


# ─────────────────────────────────────────────
# GET /api/v1/rates/{rate_id} — 查询单条报价
# ─────────────────────────────────────────────

@router.get(
    "/{rate_id}",
    response_model=RateResponse,
    summary="查询单条报价详情",
)
async def get_rate(
    rate_id: int,
    db: AsyncSession = Depends(get_db),
) -> OceanRate:

    stmt = select(OceanRate).where(OceanRate.id == rate_id)
    result = await db.execute(stmt)
    rate = result.scalar_one_or_none()

    if rate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"报价 ID {rate_id} 不存在",
        )
    return rate


# ─────────────────────────────────────────────
# POST /api/v1/rates/import/excel — Excel 批量导入
# （业务逻辑由 services/ingestion.py 实现，批次 3 填充）
# ─────────────────────────────────────────────

@router.post(
    "/import/excel",
    response_model=RateBatchImportResult,
    status_code=status.HTTP_200_OK,
    summary="Excel 批量导入报价",
    description="上传标准格式 Excel 文件，批量解析并导入报价数据。单行解析失败不中断整体任务。",
)
async def import_rates_from_excel(
    file: UploadFile = File(..., description="Excel 文件 (.xlsx)"),
    db: AsyncSession = Depends(get_db),
) -> RateBatchImportResult:

    # 文件格式校验
    if not file.filename or not file.filename.endswith(".xlsx"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="仅支持 .xlsx 格式的 Excel 文件",
        )

    logger.info(f"收到 Excel 导入请求: 文件名={file.filename}")

    # ⚠️ 业务逻辑由 ingestion.py 实现（批次 3），当前返回占位响应
    return RateBatchImportResult(
        total=0,
        success=0,
        failed=0,
        errors=[],
        source_file=file.filename,
    )
