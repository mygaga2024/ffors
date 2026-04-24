"""
FFORS 企业微信通知服务 (WeChat Work Notification)
负责将价格异常波动推送至企业微信群机器人。
遵循 DEVELOPMENT_PROTOCOL.md：
  - §1 最小干预：只负责消息推送，不混入业务逻辑
  - §6 错误处理：推送失败只记录日志，不阻断主流程
"""

from typing import Optional

import httpx

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("ffors.services.notification")

# 告警阈值：涨跌幅绝对值超过 20% 触发推送
ALERT_THRESHOLD = 0.20

# 企业微信 Webhook 基础 URL
WECOM_WEBHOOK_BASE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def _format_change(value: Optional[float]) -> str:
    """格式化变化率为百分比字符串，带颜色提示符。"""
    if value is None:
        return "暂无数据"
    pct = value * 100
    if pct > 0:
        return f"📈 +{pct:.1f}%"
    elif pct < 0:
        return f"📉 {pct:.1f}%"
    else:
        return "➡️ 持平"


def should_alert(wow_20gp: Optional[float], wow_40gp: Optional[float]) -> bool:
    """
    判断是否需要触发告警推送。
    规则：20GP 或 40GP 的环比涨跌幅绝对值 > ALERT_THRESHOLD (20%)
    """
    for change in [wow_20gp, wow_40gp]:
        if change is not None and abs(change) >= ALERT_THRESHOLD:
            return True
    return False


def build_alert_markdown(
    pol_code: str,
    pod_code: str,
    carrier: str,
    price_20gp: Optional[str],
    price_40gp: Optional[str],
    wow_20gp: Optional[float],
    wow_40gp: Optional[float],
    source_file: Optional[str] = None,
) -> str:
    """
    构造企业微信 Markdown 格式的告警消息。
    """
    lines = [
        f"## ⚠️ 海运价格异常波动告警",
        f"",
        f"> **航线**: {pol_code} → {pod_code}",
        f"> **船公司**: {carrier}",
        f"",
        f"| 箱型 | 价格 (USD) | 环比变化 |",
        f"|------|-----------|----------|",
        f"| 20GP | {price_20gp or 'N/A'} | {_format_change(wow_20gp)} |",
        f"| 40GP | {price_40gp or 'N/A'} | {_format_change(wow_40gp)} |",
    ]

    if source_file:
        lines.append(f"")
        lines.append(f"> 数据来源: `{source_file}`")

    return "\n".join(lines)


async def send_wecom_alert(
    pol_code: str,
    pod_code: str,
    carrier: str,
    price_20gp: Optional[str],
    price_40gp: Optional[str],
    wow_20gp: Optional[float],
    wow_40gp: Optional[float],
    source_file: Optional[str] = None,
) -> bool:
    """
    发送企业微信群机器人告警。
    返回 True 表示发送成功，False 表示发送失败（仅记录日志，不抛异常）。
    """
    webhook_key = settings.wecom_webhook_key
    if not webhook_key:
        logger.warning("未配置 WECOM_WEBHOOK_KEY，跳过告警推送")
        return False

    markdown_content = build_alert_markdown(
        pol_code=pol_code,
        pod_code=pod_code,
        carrier=carrier,
        price_20gp=price_20gp,
        price_40gp=price_40gp,
        wow_20gp=wow_20gp,
        wow_40gp=wow_40gp,
        source_file=source_file,
    )

    payload = {
        "msgtype": "markdown",
        "markdown": {"content": markdown_content},
    }

    url = f"{WECOM_WEBHOOK_BASE}?key={webhook_key}"

    try:
        # 尊重代理配置 (DEVELOPMENT_PROTOCOL.md §5)
        proxies = settings.http_proxy or None

        async with httpx.AsyncClient(proxy=proxies, timeout=10.0) as client:
            response = await client.post(url, json=payload)
            result = response.json()

        if result.get("errcode") == 0:
            logger.info(
                f"告警推送成功: {pol_code}→{pod_code} {carrier}"
            )
            return True
        else:
            logger.error(
                f"告警推送失败: errcode={result.get('errcode')}, "
                f"errmsg={result.get('errmsg')}"
            )
            return False

    except Exception as e:
        logger.error(f"告警推送异常: {e}")
        return False


async def send_wecom_report(markdown_content: str) -> bool:
    """
    发送通用的企业微信群机器人报告（如晨报）。
    """
    webhook_key = settings.wecom_webhook_key
    if not webhook_key:
        logger.warning("未配置 WECOM_WEBHOOK_KEY，跳过晨报推送")
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {"content": markdown_content},
    }

    url = f"{WECOM_WEBHOOK_BASE}?key={webhook_key}"

    try:
        proxies = settings.http_proxy or None

        async with httpx.AsyncClient(proxy=proxies, timeout=15.0) as client:
            response = await client.post(url, json=payload)
            result = response.json()

        if result.get("errcode") == 0:
            logger.info("晨报推送成功")
            return True
        else:
            logger.error(
                f"晨报推送失败: errcode={result.get('errcode')}, "
                f"errmsg={result.get('errmsg')}"
            )
            return False

    except Exception as e:
        logger.error(f"晨报推送异常: {e}")
        return False
