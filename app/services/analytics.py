"""
FFORS 量化分析服务 (Analytics Service)
负责计算海运报价的环比 (WoW) 和月同比 (MoM) 价格变化率。

策略：
  - 基准价格：分别计算 20GP 和 40GP 的波动率
  - 时间基准：方案 A 变体 — 按同航线 (POL+POD+Carrier) 的入库时间
    倒序查找最近一笔历史报价进行对比
  - WoW：对比 7 天内的最近一笔同航线报价
  - MoM：对比 30 天内的最近一笔同航线报价

遵循 DEVELOPMENT_PROTOCOL.md：
  - §1 最小干预：只负责价格波动计算
  - §4 不混入通知或 AI 行情逻辑
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rate import OceanRate
from app.utils.logger import get_logger

logger = get_logger("ffors.services.analytics")


def _calc_change_rate(
    current: Optional[Decimal],
    previous: Optional[Decimal],
) -> Optional[float]:
    """
    计算价格变化率（百分比）。
    返回 None 的场景：
      - 当前或历史价格为空
      - 历史价格为 0（避免除零）
    返回值示例：0.2 表示 +20%，-0.15 表示 -15%
    """
    if current is None or previous is None:
        return None
    if previous == 0:
        return None
    try:
        rate = float((current - previous) / previous)
        return round(rate, 4)
    except Exception:
        return None


async def _find_historical_rate(
    db: AsyncSession,
    pol_code: str,
    pod_code: str,
    carrier: str,
    before_time: datetime,
    lookback_days: int,
) -> Optional[OceanRate]:
    """
    查找指定航线在 lookback_days 天内、before_time 之前的最近一条报价。
    """
    cutoff = before_time - timedelta(days=lookback_days)

    stmt = (
        select(OceanRate)
        .where(
            and_(
                OceanRate.pol_code == pol_code,
                OceanRate.pod_code == pod_code,
                OceanRate.carrier == carrier,
                OceanRate.created_at < before_time,
                OceanRate.created_at >= cutoff,
            )
        )
        .order_by(desc(OceanRate.created_at))
        .limit(1)
    )

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def calculate_price_changes(
    db: AsyncSession,
    new_rates: list[OceanRate],
) -> None:
    """
    为本次导入的新报价列表计算 WoW 和 MoM 变化率，
    直接回填到每个 OceanRate 实例的属性中（尚未提交到数据库）。

    Args:
        db: 异步数据库 Session（已激活）
        new_rates: 本批次新构造的 OceanRate 对象列表
    """
    if not new_rates:
        return

    now = datetime.now(timezone.utc)
    updated_count = 0

    for rate in new_rates:
        # --- WoW: 7 天窗口 ---
        wow_ref = await _find_historical_rate(
            db,
            pol_code=rate.pol_code,
            pod_code=rate.pod_code,
            carrier=rate.carrier,
            before_time=now,
            lookback_days=7,
        )

        if wow_ref:
            rate.wow_20gp = _calc_change_rate(rate.price_20gp, wow_ref.price_20gp)
            rate.wow_40gp = _calc_change_rate(rate.price_40gp, wow_ref.price_40gp)

        # --- MoM: 30 天窗口 ---
        mom_ref = await _find_historical_rate(
            db,
            pol_code=rate.pol_code,
            pod_code=rate.pod_code,
            carrier=rate.carrier,
            before_time=now,
            lookback_days=30,
        )

        if mom_ref:
            rate.mom_20gp = _calc_change_rate(rate.price_20gp, mom_ref.price_20gp)
            rate.mom_40gp = _calc_change_rate(rate.price_40gp, mom_ref.price_40gp)

        if any([rate.wow_20gp, rate.mom_20gp, rate.wow_40gp, rate.mom_40gp]):
            updated_count += 1

    logger.info(
        f"价格波动计算完成: {updated_count}/{len(new_rates)} 条有历史对比数据"
    )
