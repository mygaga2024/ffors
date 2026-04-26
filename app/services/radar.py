"""
FFORS 比价雷达服务 (Price Radar)
为业务端提供多维度的性价比打分方案推荐，并结合 RAG 交叉验证风险。
"""

import httpx
from pydantic import BaseModel
from typing import Optional, List

from app.config import settings
from app.models.rate import OceanRate
from app.services.rag import search_knowledge
from app.utils.logger import get_logger

logger = get_logger("ffors.services.radar")


class RadarRecommendation(BaseModel):
    carrier: str
    price: float
    route_type: Optional[str]
    transit_port: Optional[str]
    etd_weekday: Optional[str]
    tt_days: Optional[int]
    validity_period: Optional[str]
    stability_score: float  # 波动率越小分数越高
    risk_score: float       # 风险越小分数越高
    total_score: float      # 满分 100
    tags: List[str]         # 如 "最佳价格", "综合最优"
    remarks: Optional[str]


def _calculate_score(
    price: float,
    min_price: float,
    tt_days: Optional[int],
    min_tt_days: Optional[int],
    wow: Optional[float],
    risk: Optional[float]
) -> float:
    """
    计算性价比得分。满分 100 分。
    权重配置: 价格 40%, 时效 30%, 稳定性 20%, 风险极低 10%
    """
    score = 0.0
    
    # 1. 价格 (40分): 与最低价越近分数越高
    if price > 0 and min_price > 0:
        price_ratio = min_price / price
        score += price_ratio * 40.0
        
    # 2. 时效 (30分): 假设没填 tt_days 的得及格分 15 分
    if tt_days and tt_days > 0 and min_tt_days and min_tt_days > 0:
        tt_ratio = min_tt_days / tt_days
        score += tt_ratio * 30.0
    else:
        score += 15.0 
        
    # 3. 稳定性 (20分): wow 绝对值越小越稳定
    wow_val = abs(wow) if wow is not None else 0.0
    # wow > 20% 时稳定得分为 0，wow=0 时得满分 20
    stability = max(0.0, 20.0 - (wow_val * 100))
    score += stability
    
    # 4. 风险评分 (10分): risk_score (0-100)，越小越好
    risk_val = risk if risk is not None else 50.0  # 默认风险中等
    ai_risk_score = max(0.0, 10.0 - (risk_val / 10.0))
    score += ai_risk_score
    
    return round(score, 2)


async def cross_validate_risk_with_minimax(pol: str, pod: str, raw_context: str) -> str:
    """
    调用大模型，要求其对不同新闻源的动态进行交叉比对，鉴别可靠性。
    """
    if not raw_context.strip():
        return "暂无相关航运动态。"
        
    api_key = settings.minimax_api_key
    if not api_key:
        return "受限于 API Key 配置，暂不提供 AI 风险交叉验证。"
        
    url = f"{settings.minimax_base_url}/text/chatcompletion_pro?GroupId={settings.minimax_group_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    prompt = f"""请作为资深货代风控官，基于以下来自不同数据源的 RAG 检索上下文，评估从 {pol} 到 {pod} 航线的潜在延误或风险。
必须进行交叉验证！如果信息源单一且极端，请提示风险不确定；如果多个源证实（如罢工、塞港），请发出明确预警。
不要废话，输出不超过 100 字的精炼锦囊。

【检索上下文】
{raw_context}
"""
    payload = {
        "model": "MiniMax-Text-01",
        "messages": [{"sender_type": "USER", "sender_name": "RiskEngine", "text": prompt}],
        "reply_constraints": {"sender_type": "BOT", "sender_name": "Analyst"},
    }

    try:
        proxies = settings.http_proxy or None
        async with httpx.AsyncClient(proxy=proxies, timeout=10.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            if "choices" in data and data["choices"]:
                return data["choices"][0]["messages"][0]["text"].strip()
    except Exception as e:
        logger.error(f"风险交叉验证 AI 调用失败: {e}")
    
    return "风险交叉验证服务暂不可用。"


async def get_route_recommendations(
    rates: List[OceanRate], 
    container_type: str,
    pol_code: str,
    pod_code: str
) -> dict:
    """
    接收一组同航线有效报价，过滤、评分、打标，并返回带风险锦囊的雷达报告。
    """
    if not rates:
        return {"recommendations": [], "risk_insight": "暂无匹配报价。"}
        
    valid_rates = []
    # 提取目标箱型的价格
    for r in rates:
        price = None
        wow = None
        if container_type == "20GP" and r.price_20gp:
            price = float(r.price_20gp)
            wow = r.wow_20gp
        elif container_type == "40GP" and r.price_40gp:
            price = float(r.price_40gp)
            wow = r.wow_40gp
        elif container_type == "40HQ" and r.price_40hq:
            price = float(r.price_40hq)
            wow = r.wow_40gp  # 40HQ 暂时借用 40GP 的 wow，或者忽略

        if price:
            # 格式化日期为星期
            etd_weekday = None
            if r.etd:
                weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                etd_weekday = weekdays[r.etd.weekday()]

            # 格式化有效期
            validity_period = None
            if r.valid_from and r.valid_to:
                validity_period = f"{r.valid_from.strftime('%Y.%m.%d')}-{r.valid_to.strftime('%Y.%m.%d')}"
                
            valid_rates.append({
                "carrier": r.carrier,
                "price": price,
                "route_type": getattr(r, "route_type", None),
                "transit_port": getattr(r, "transit_port", None),
                "etd_weekday": etd_weekday,
                "tt_days": r.tt_days,
                "validity_period": validity_period,
                "wow": wow,
                "risk": r.risk_score,
                "remarks": r.remarks or ""
            })
            
    if not valid_rates:
         return {"recommendations": [], "risk_insight": "暂无该箱型报价。"}

    # 找出最低价和最快时效用于归一化
    min_price = min(r["price"] for r in valid_rates)
    # tt_days 可能为空
    tt_days_list = [r["tt_days"] for r in valid_rates if r["tt_days"] and r["tt_days"] > 0]
    min_tt_days = min(tt_days_list) if tt_days_list else None
    
    recommendations = []
    for r in valid_rates:
        total_score = _calculate_score(
            price=r["price"],
            min_price=min_price,
            tt_days=r["tt_days"],
            min_tt_days=min_tt_days,
            wow=r["wow"],
            risk=r["risk"]
        )
        # 将 wow 转换为单项稳定性得分用于展示
        wow_val = abs(r["wow"]) if r["wow"] is not None else 0.0
        stability_score = max(0.0, 100.0 - (wow_val * 500))  # 转换到百分制展示
        
        recommendations.append(RadarRecommendation(
            carrier=r["carrier"],
            price=r["price"],
            route_type=r["route_type"],
            transit_port=r["transit_port"],
            etd_weekday=r["etd_weekday"],
            tt_days=r["tt_days"],
            validity_period=r["validity_period"],
            stability_score=round(stability_score, 1),
            risk_score=r["risk"] if r["risk"] is not None else 50.0,
            total_score=total_score,
            tags=[],
            remarks=r["remarks"]
        ))
        
    # 排序与打标
    recommendations.sort(key=lambda x: x.total_score, reverse=True)
    
    if recommendations:
        top_rec = recommendations[0]
        # 将综合最优追加到备注中
        if top_rec.remarks:
            top_rec.remarks += " | 👑 综合最优"
        else:
            top_rec.remarks = "👑 综合最优"
        
        # 找绝对低价
        cheapest = min(recommendations, key=lambda x: x.price)
        if cheapest != recommendations[0]:
            cheapest.tags.append("💰 价格极客")
            
        # 找最快时效
        fastest = [r for r in recommendations if r.tt_days is not None]
        if fastest:
            fastest_rec = min(fastest, key=lambda x: x.tt_days)
            if fastest_rec != recommendations[0] and fastest_rec.tt_days < (recommendations[0].tt_days or 999):
                 fastest_rec.tags.append("⚡ 时效王者")

    # 并发进行 RAG 检索并让大模型交叉验证
    query = f"{pol_code} 到 {pod_code} 的海运航线近期动态、罢工、塞港"
    raw_context = search_knowledge(query, top_k=5)
    risk_insight = await cross_validate_risk_with_minimax(pol_code, pod_code, raw_context)
    
    return {
        "recommendations": [r.model_dump() for r in recommendations],
        "risk_insight": risk_insight
    }
