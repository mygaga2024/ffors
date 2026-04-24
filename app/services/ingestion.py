"""
FFORS Excel 数据导入服务 (Ingestion Service)
负责：解析 Excel → 标准化映射 → 批量写入数据库
遵循 DEVELOPMENT_PROTOCOL.md：
  - §1 最小干预：只负责数据清洗与转换，不混入推送/分析逻辑
  - §4 数据接入层：不得混入通知或 AI 行情逻辑
  - §6 错误处理：单条失败不中断批量任务，跳过并记录
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.port import Port
from app.models.rate import OceanRate
from app.schemas.rate import RateBatchImportResult, ImportError as ImportErrorSchema
from app.services.analytics import calculate_price_changes
from app.services.notification import should_alert, send_wecom_alert
from app.utils.logger import get_logger

logger = get_logger("ffors.services.ingestion")

# ─────────────────────────────────────────────
# Excel 列名映射（英文列名 → ORM 字段名）
# ─────────────────────────────────────────────

COLUMN_MAP = {
    "POL": "pol_code",
    "POD": "pod_code",
    "Carrier": "carrier",
    "Vendor": "vendor_name",
    "20GP": "price_20gp",
    "40GP": "price_40gp",
    "40HQ": "price_40hq",
    "Currency": "currency",
    "ETD": "etd",
    "TT(Days)": "tt_days",
    "TT": "tt_days",
    "Valid From": "valid_from",
    "Valid To": "valid_to",
    "Remarks": "remarks",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """将 Excel 列名标准化为 ORM 字段名，忽略未知列。"""
    # 去除列名前后空格
    df.columns = [str(c).strip() for c in df.columns]

    rename_map = {}
    for col in df.columns:
        # 精确匹配
        if col in COLUMN_MAP:
            rename_map[col] = COLUMN_MAP[col]
        # 不区分大小写匹配
        else:
            for excel_col, orm_field in COLUMN_MAP.items():
                if col.upper() == excel_col.upper():
                    rename_map[col] = orm_field
                    break

    df = df.rename(columns=rename_map)
    return df


# ─────────────────────────────────────────────
# 港口名称标准化
# ─────────────────────────────────────────────

async def _build_port_lookup(db: AsyncSession) -> dict[str, str]:
    """
    从数据库加载港口数据，构建 别名→标准代码 的查找表。
    例如：{"SHA": "CNSHA", "上海港": "CNSHA", "CNSHA": "CNSHA"}
    """
    result = await db.execute(select(Port))
    ports = result.scalars().all()

    lookup: dict[str, str] = {}
    for port in ports:
        # 代码本身
        lookup[port.code.upper()] = port.code
        # 英文名
        if port.name_en:
            lookup[port.name_en.upper()] = port.code
        # 中文名
        if port.name_cn:
            lookup[port.name_cn] = port.code
        # 别名列表
        if port.aliases:
            for alias in port.aliases:
                lookup[str(alias).upper()] = port.code

    return lookup


def normalize_port_name(raw: str, lookup: dict[str, str]) -> Optional[str]:
    """
    将原始港口名称/代码转换为标准 UN/LOCODE 代码。
    返回 None 表示未匹配（会被记录为 warning，不丢弃数据）。
    """
    if not raw or not isinstance(raw, str):
        return None

    cleaned = raw.strip().upper()

    # 直接匹配
    if cleaned in lookup:
        return lookup[cleaned]

    # 去除常见后缀再试 (如 "PORT", "HARBOR")
    for suffix in [" PORT", " HARBOR", " HARBOUR", "港"]:
        stripped = cleaned.rstrip(suffix).strip()
        if stripped and stripped in lookup:
            return lookup[stripped]

    return None


# ─────────────────────────────────────────────
# 安全的类型转换
# ─────────────────────────────────────────────

def _safe_decimal(val) -> Optional[Decimal]:
    """安全转换为 Decimal，非法值返回 None。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        d = Decimal(str(val)).quantize(Decimal("0.01"))
        if d < 0:
            return None
        return d
    except (InvalidOperation, ValueError):
        return None


def _safe_int(val) -> Optional[int]:
    """安全转换为 int，非法值返回 None。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _safe_date(val):
    """安全转换为 date 对象。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, pd.Timestamp):
        return val.date()
    try:
        return pd.to_datetime(str(val)).date()
    except Exception:
        return None


# ─────────────────────────────────────────────
# 核心导入函数
# ─────────────────────────────────────────────

def parse_excel(file_bytes: bytes) -> pd.DataFrame:
    """
    解析 Excel 文件内容为 DataFrame。
    使用 openpyxl 引擎读取 .xlsx 格式。
    """
    df = pd.read_excel(BytesIO(file_bytes), engine="openpyxl")

    if df.empty:
        raise ValueError("Excel 文件为空或未包含有效数据行")

    # 标准化列名
    df = _normalize_columns(df)

    logger.info(f"Excel 解析完成: {len(df)} 行, 列名: {list(df.columns)}")
    return df


async def _trigger_alerts_and_ai(alert_rates: list[OceanRate]) -> None:
    """
    异步后台任务：对达到告警阈值的报价执行通知推送和 AI 分析。
    此函数在 db.commit() 之后被 asyncio.create_task 调用，
    不会阻塞导入 API 的响应。
    """
    import asyncio
    from app.models.base import get_async_session
    from app.services.ai_analyzer import batch_analyze_alerts

    # --- 1. 企业微信告警推送（立即执行） ---
    for rate in alert_rates:
        try:
            await send_wecom_alert(
                pol_code=rate.pol_code,
                pod_code=rate.pod_code,
                carrier=rate.carrier,
                price_20gp=str(rate.price_20gp) if rate.price_20gp else None,
                price_40gp=str(rate.price_40gp) if rate.price_40gp else None,
                wow_20gp=rate.wow_20gp,
                wow_40gp=rate.wow_40gp,
                source_file=rate.source_file,
            )
        except Exception as e:
            logger.error(f"告警推送异常: {rate.pol_code}→{rate.pod_code}, {e}")

    # --- 2. AI 分析（后台静默执行） ---
    try:
        session_factory = get_async_session()
        async with session_factory() as db:
            # 重新从数据库加载这些 rate（因为上面的 session 已关闭）
            from sqlalchemy import select
            rate_ids = [r.id for r in alert_rates if r.id]
            if rate_ids:
                from app.models.rate import OceanRate as RateModel
                stmt = select(RateModel).where(RateModel.id.in_(rate_ids))
                result = await db.execute(stmt)
                fresh_rates = list(result.scalars().all())
                await batch_analyze_alerts(fresh_rates, db)
    except Exception as e:
        logger.error(f"后台 AI 分析异常: {e}")


async def batch_import_rates(
    df: pd.DataFrame,
    source_file: str,
    db: AsyncSession,
) -> RateBatchImportResult:
    """
    将 DataFrame 中的报价数据批量写入数据库。
    流程：解析行 → 构造 ORM → 计算 WoW/MoM → 提交入库 → 触发告警+AI。
    单条失败不中断整体任务（遵循 DEVELOPMENT_PROTOCOL.md §6）。
    """
    import asyncio

    port_lookup = await _build_port_lookup(db)

    total = len(df)
    success = 0
    errors: list[ImportErrorSchema] = []
    new_rates: list[OceanRate] = []

    for idx, row in df.iterrows():
        row_num = int(idx) + 2  # Excel 行号（第 1 行是表头）
        try:
            row_dict = row.to_dict()

            # --- 必填字段校验 ---
            raw_pol = str(row_dict.get("pol_code", "")).strip()
            raw_pod = str(row_dict.get("pod_code", "")).strip()
            carrier = str(row_dict.get("carrier", "")).strip()

            if not raw_pol or not raw_pod or not carrier:
                errors.append(ImportErrorSchema(
                    row=row_num,
                    reason="缺少必填字段: POL、POD 或 Carrier 为空",
                ))
                continue

            # --- 港口标准化 ---
            pol_code = normalize_port_name(raw_pol, port_lookup)
            pod_code = normalize_port_name(raw_pod, port_lookup)

            if pol_code is None:
                logger.warning(f"行 {row_num}: 未知起运港 '{raw_pol}'，保留原始值")
                pol_code = raw_pol.upper()

            if pod_code is None:
                logger.warning(f"行 {row_num}: 未知目的港 '{raw_pod}'，保留原始值")
                pod_code = raw_pod.upper()

            # --- 构造 ORM 对象 ---
            rate = OceanRate(
                pol_code=pol_code,
                pod_code=pod_code,
                carrier=carrier,
                price_20gp=_safe_decimal(row_dict.get("price_20gp")),
                price_40gp=_safe_decimal(row_dict.get("price_40gp")),
                price_40hq=_safe_decimal(row_dict.get("price_40hq")),
                currency=str(row_dict.get("currency", "USD")).strip().upper() or "USD",
                etd=_safe_date(row_dict.get("etd")),
                tt_days=_safe_int(row_dict.get("tt_days")),
                valid_from=_safe_date(row_dict.get("valid_from")),
                valid_to=_safe_date(row_dict.get("valid_to")),
                remarks=str(row_dict.get("remarks", "")).strip() or None,
                source_file=source_file,
                created_at=datetime.now(timezone.utc),
            )

            db.add(rate)
            new_rates.append(rate)
            success += 1

        except Exception as e:
            logger.error(f"行 {row_num} 导入失败: {e}")
            errors.append(ImportErrorSchema(
                row=row_num,
                reason=str(e),
            ))

    # --- 计算 WoW / MoM 价格波动 ---
    if new_rates:
        try:
            await calculate_price_changes(db, new_rates)
        except Exception as e:
            logger.warning(f"价格波动计算异常（不影响导入）: {e}")

    # 统一提交（所有成功行一次性入库）
    if success > 0:
        try:
            await db.commit()
            logger.info(f"批量导入提交成功: {success}/{total}")
        except Exception as e:
            await db.rollback()
            logger.error(f"数据库提交失败: {e}")
            return RateBatchImportResult(
                total=total,
                success=0,
                failed=total,
                errors=[ImportErrorSchema(row=0, reason=f"数据库提交失败: {e}")],
                source_file=source_file,
            )

        # --- 筛选告警记录并触发异步后台任务 ---
        alert_rates = [
            r for r in new_rates
            if should_alert(r.wow_20gp, r.wow_40gp)
        ]
        if alert_rates:
            logger.info(f"检测到 {len(alert_rates)} 条价格异常波动记录，触发告警")
            asyncio.create_task(_trigger_alerts_and_ai(alert_rates))

    return RateBatchImportResult(
        total=total,
        success=success,
        failed=len(errors),
        errors=errors,
        source_file=source_file,
    )


