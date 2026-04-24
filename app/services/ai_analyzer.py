"""
FFORS AI 行情分析服务 (MiniMax Integration)
负责调用大模型对价格异常波动进行智能分析并回填结果。
遵循 DEVELOPMENT_PROTOCOL.md：
  - §1 最小干预：只负责 AI 调用与结果存储
  - §5 代理配置感知：尊重 HTTP_PROXY 环境变量
  - §6 错误处理：AI 调用失败只记录日志，不阻断主流程
"""

import json
import re
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.rate import OceanRate
from app.utils.logger import get_logger

logger = get_logger("ffors.services.ai_analyzer")


def _build_prompt(rate: OceanRate) -> str:
    """
    构造自然语言风格的分析 Prompt。
    风格：像一个货代同事在问市场分析师问题。
    """
    parts = [
        f"你是一个资深的海运市场分析师。",
        f"",
        f"我这里有一条新的海运报价数据：",
        f"- 航线：{rate.pol_code} → {rate.pod_code}",
        f"- 船公司：{rate.carrier}",
    ]

    if rate.price_20gp is not None:
        parts.append(f"- 20GP 价格：${rate.price_20gp}")
    if rate.price_40gp is not None:
        parts.append(f"- 40GP 价格：${rate.price_40gp}")

    parts.append("")

    # 自然提问风格
    changes = []
    if rate.wow_20gp is not None:
        pct = rate.wow_20gp * 100
        direction = "涨了" if pct > 0 else "跌了"
        changes.append(f"20GP 相比上周{direction} {abs(pct):.1f}%")
    if rate.wow_40gp is not None:
        pct = rate.wow_40gp * 100
        direction = "涨了" if pct > 0 else "跌了"
        changes.append(f"40GP 相比上周{direction} {abs(pct):.1f}%")

    if changes:
        parts.append(f"这个价格相比上周怎么样？具体来说，{'，'.join(changes)}。")
    else:
        parts.append("这个价格相比上周怎么样？")

    parts.extend([
        "",
        "请你帮我分析：",
        "1. 可能的涨价/跌价原因（从供需、旺季淡季、运力调整等角度）",
        "2. 给这个航线一个风险评分（0-100，0 代表低风险即价格稳定，100 代表高风险即价格剧烈波动）",
        "",
        "请用以下 JSON 格式回复：",
        '{"risk_score": <0-100的整数>, "summary": "<50字以内的中文分析摘要>"}',
    ])

    return "\n".join(parts)


def _parse_ai_response(text: str) -> tuple[Optional[float], Optional[str]]:
    """
    从 AI 响应文本中解析 risk_score 和 summary。
    容错设计：如果 AI 返回了非标准格式，也尽量提取有效信息。
    """
    # 尝试从文本中提取 JSON 块
    json_match = re.search(r"\{[^}]+\}", text)
    if json_match:
        try:
            data = json.loads(json_match.group())
            risk_score = data.get("risk_score")
            summary = data.get("summary", "")

            # 校验 risk_score 范围
            if risk_score is not None:
                risk_score = max(0, min(100, float(risk_score)))

            # 截断过长摘要
            if summary and len(summary) > 200:
                summary = summary[:197] + "..."

            return risk_score, summary
        except (json.JSONDecodeError, ValueError):
            pass

    # 兜底：直接将全文作为摘要
    logger.warning("AI 返回了非标准格式，使用原文作为摘要")
    truncated = text[:200] if len(text) > 200 else text
    return None, truncated


async def analyze_rate(rate: OceanRate, db: AsyncSession) -> bool:
    """
    调用 MiniMax 大模型分析单条报价的价格波动。
    结果直接回填到 rate.risk_score 和 rate.ai_summary，并提交更新。
    返回 True 表示分析成功。
    """
    api_key = settings.minimax_api_key
    base_url = settings.minimax_base_url
    group_id = settings.minimax_group_id

    if not api_key:
        logger.warning("未配置 MINIMAX_API_KEY，跳过 AI 分析")
        return False

    prompt = _build_prompt(rate)

    # MiniMax ChatCompletion Pro API
    url = f"{base_url}/text/chatcompletion_pro?GroupId={group_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "MiniMax-Text-01",
        "messages": [
            {
                "sender_type": "USER",
                "sender_name": "FFORS",
                "text": prompt,
            }
        ],
        "reply_constraints": {"sender_type": "BOT", "sender_name": "Analyst"},
        "tokens_to_generate": 512,
        "temperature": 0.3,
    }

    try:
        proxies = settings.http_proxy or None

        async with httpx.AsyncClient(proxy=proxies, timeout=30.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()

        # 提取回复内容
        reply_text = ""
        choices = result.get("choices", [])
        if choices:
            messages = choices[0].get("messages", [])
            if messages:
                reply_text = messages[0].get("text", "")

        if not reply_text:
            logger.warning(f"AI 返回空内容: rate_id={rate.id}")
            return False

        # 解析结果
        risk_score, summary = _parse_ai_response(reply_text)

        # 回填数据
        rate.risk_score = risk_score
        rate.ai_summary = summary
        await db.commit()

        logger.info(
            f"AI 分析完成: {rate.pol_code}→{rate.pod_code} {rate.carrier}, "
            f"risk={risk_score}, summary={summary[:30] if summary else 'N/A'}..."
        )
        return True

    except httpx.HTTPStatusError as e:
        logger.error(f"MiniMax API 错误: {e.response.status_code} - {e.response.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"AI 分析异常: {e}")
        return False


async def batch_analyze_alerts(
    alert_rates: list[OceanRate],
    db: AsyncSession,
) -> int:
    """
    批量对触发告警的报价进行 AI 分析（异步后台任务入口）。
    返回成功分析的条数。
    """
    if not alert_rates:
        return 0

    success_count = 0
    for rate in alert_rates:
        try:
            ok = await analyze_rate(rate, db)
            if ok:
                success_count += 1
        except Exception as e:
            logger.error(f"批量分析中单条异常: rate_id={rate.id}, error={e}")

    logger.info(f"批量 AI 分析完成: {success_count}/{len(alert_rates)} 成功")
    return success_count
