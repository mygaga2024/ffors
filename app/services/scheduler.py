"""
FFORS 定时调度服务 (APScheduler)
负责每日晨报推送及其他周期性任务。
"""

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, func

from app.models.base import get_async_session
from app.models.rate import OceanRate
from app.services.notification import send_wecom_report
from app.utils.logger import get_logger

logger = get_logger("ffors.services.scheduler")

# 单例调度器实例
_scheduler: AsyncIOScheduler | None = None


async def generate_morning_report():
    """
    生成并发送每日晨报：汇总过去 24 小时内更新的航线报价。
    """
    logger.info("开始生成 FFORS 每日晨报...")
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    session_factory = get_async_session()
    async with session_factory() as db:
        # 查询过去 24 小时内入库的有效报价
        stmt = select(OceanRate).where(OceanRate.created_at >= yesterday)
        result = await db.execute(stmt)
        rates = result.scalars().all()

    if not rates:
        logger.info("过去 24 小时内无新报价，跳过晨报推送。")
        return

    # 简单统计
    total_new = len(rates)
    pol_pod_pairs = set(f"{r.pol_code}→{r.pod_code}" for r in rates)
    
    # 提取有严重风险或剧烈波动的（示例条件：risk_score >= 80 或 波动>20%）
    high_risk_rates = [
        r for r in rates
        if (r.risk_score and r.risk_score >= 80) or 
           (r.wow_20gp and abs(r.wow_20gp) >= 0.2) or
           (r.wow_40gp and abs(r.wow_40gp) >= 0.2)
    ]

    # 构建 Markdown
    lines = [
        "## 🌅 FFORS 每日海运报价晨报",
        f"> **统计时间**: {yesterday.strftime('%Y-%m-%d %H:%00')} 至 {now.strftime('%Y-%m-%d %H:%00')} (UTC)",
        "",
        f"**昨日新增报价**: {total_new} 条",
        f"**涉及航线数量**: {len(pol_pod_pairs)} 条",
        "",
    ]

    if high_risk_rates:
        lines.append("### ⚠️ 重点关注 (高波动/高风险)")
        for r in high_risk_rates[:5]:  # 最多展示 5 条
            price_str = f"20GP: ${r.price_20gp}" if r.price_20gp else f"40GP: ${r.price_40gp}"
            lines.append(f"- **{r.pol_code}→{r.pod_code}** ({r.carrier}): {price_str}")
            if r.ai_summary:
                lines.append(f"  > 🤖 {r.ai_summary}")
        if len(high_risk_rates) > 5:
            lines.append(f"- *... 及其他 {len(high_risk_rates) - 5} 条异常波动记录*")
    else:
        lines.append("✅ *昨日市场报价整体平稳，未检测到剧烈波动。*")

    markdown_content = "\n".join(lines)
    
    await send_wecom_report(markdown_content)
    logger.info("晨报生成并推送完成。")


def start_scheduler():
    """启动全局定时调度器。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
        
        # 每天早上 9:00 (根据服务器时区，通常是 UTC，此处设定为固定的 UTC 小时。如果服务器是北京时间，可按需调整)
        # 此处使用简单的 cron 表达式
        _scheduler.add_job(
            generate_morning_report,
            "cron",
            hour=1,  # UTC 1:00 = 北京时间 9:00
            minute=0,
            id="morning_report",
            replace_existing=True,
        )
        
        _scheduler.start()
        logger.info("定时调度器已启动 (APScheduler)。")


def stop_scheduler():
    """停止全局定时调度器。"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown()
        logger.info("定时调度器已停止。")
