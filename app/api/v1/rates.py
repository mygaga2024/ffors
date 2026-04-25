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
from app.services.ingestion import parse_excel, batch_import_rates
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
# GET /api/v1/rates/radar/recommendations — 比价雷达
# ─────────────────────────────────────────────

@router.get(
    "/radar/recommendations",
    summary="比价雷达 (智能推优与风险排查)",
    description="给定航线与箱型，拉取数据库报价并进行性价比智能打分，同时交叉验证航线风险。",
)
async def get_radar_recommendations(
    pol_code: str = Query(..., min_length=2, max_length=50, description="起运港代码"),
    pod_code: str = Query(..., min_length=2, max_length=50, description="目的港代码"),
    container_type: str = Query("40HQ", description="箱型: 20GP/40GP/40HQ"),
    db: AsyncSession = Depends(get_db),
):
    pol_code = pol_code.strip().upper()
    pod_code = pod_code.strip().upper()
    container_type = container_type.strip().upper()
    
    if container_type not in ["20GP", "40GP", "40HQ"]:
        raise HTTPException(status_code=400, detail="不支持的箱型，请选择 20GP/40GP/40HQ")
        
    stmt = (
        select(OceanRate)
        .where(
            and_(
                OceanRate.pol_code == pol_code,
                OceanRate.pod_code == pod_code
            )
        )
    )
    result = await db.execute(stmt)
    rates = result.scalars().all()
    
    from app.services.radar import get_route_recommendations
    return await get_route_recommendations(list(rates), container_type, pol_code, pod_code)


# ─────────────────────────────────────────────
# POST /api/v1/rates/import/excel — Excel 批量导入
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

    # 读取文件内容
    try:
        file_bytes = await file.read()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"文件读取失败: {e}",
        )

    # 解析 Excel
    try:
        df = parse_excel(file_bytes)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Excel 解析异常: {e}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Excel 文件解析失败: {e}",
        )

    # 批量导入
    result = await batch_import_rates(df, source_file=file.filename, db=db)
    logger.info(
        f"导入完成: {result.success}/{result.total} 成功, "
        f"{result.failed} 失败, 文件={file.filename}"
    )
    return result


# ─────────────────────────────────────────────
# POST /api/v1/rates/import/pdf — PDF 智能解析导入
# ─────────────────────────────────────────────

@router.post(
    "/import/pdf",
    response_model=RateBatchImportResult,
    status_code=status.HTTP_200_OK,
    summary="PDF 智能解析并导入报价",
    description="利用 MiniMax 大模型提取 PDF 报价单，并复用 Excel 导入逻辑入库。",
)
async def import_rates_from_pdf(
    file: UploadFile = File(..., description="PDF 报价单文件 (.pdf)"),
    db: AsyncSession = Depends(get_db),
) -> RateBatchImportResult:

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="仅支持 .pdf 格式的文件",
        )

    logger.info(f"收到 PDF 解析请求: 文件名={file.filename}")

    try:
        file_bytes = await file.read()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"文件读取失败: {e}",
        )

    # 调用 PDF 智能解析器 (转换为 DataFrame)
    from app.services.pdf_parser import parse_pdf_to_dataframe
    try:
        df = await parse_pdf_to_dataframe(file_bytes)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"PDF AI 解析异常: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PDF 智能解析失败: {e}",
        )

    # 复用批处理入库逻辑 (自动触发港口映射、WoW/MoM计算和告警)
    result = await batch_import_rates(df, source_file=file.filename, db=db)
    logger.info(
        f"PDF 导入完成: {result.success}/{result.total} 成功, "
        f"{result.failed} 失败, 文件={file.filename}"
    )
    return result
