"""
FFORS 机器人交互接口 (Bot Webhooks)
接收来自钉钉或企业微信的 POST 消息，通过意图解析后触发比价雷达或运价录入。
"""

import base64
import hashlib
import hmac
import httpx
from typing import Any, Dict
from fastapi import APIRouter, Header, HTTPException, Request, BackgroundTasks
from sqlalchemy import select

from app.config import settings
from app.models.base import get_async_session
from app.models.rate import OceanRate
from app.models.port import Port
from app.services.intent import parse_intent
from app.services.radar import get_route_recommendations
from app.utils.logger import get_logger

logger = get_logger("ffors.api.bot")
router = APIRouter(prefix="/bot", tags=["Bot Interaction"])


# ─────────────────────────────────────────────
# 钉钉机器人验签中间件
# ─────────────────────────────────────────────

def verify_dingtalk_signature(timestamp: str, sign: str) -> bool:
    """验证钉钉请求签名"""
    app_secret = settings.dingtalk_app_secret
    if not app_secret:
        logger.warning("未配置 DINGTALK_APP_SECRET，跳过钉钉验签 (仅限调试)")
        return True
        
    string_to_sign = f"{timestamp}\n{app_secret}"
    hmac_code = hmac.new(
        app_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256
    ).digest()
    expected_sign = base64.b64encode(hmac_code).decode("utf-8")
    
    return expected_sign == sign


# ─────────────────────────────────────────────
# 钉钉群聊交互接口
# ─────────────────────────────────────────────

@router.post("/dingtalk/receive", summary="钉钉机器人回调")
async def receive_dingtalk_message(
    request: Request,
    background_tasks: BackgroundTasks,
    timestamp: str = Header(None),
    sign: str = Header(None)
) -> Dict[str, Any]:
    """处理钉钉机器人发来的群聊@消息。"""
    if timestamp and sign:
        if not verify_dingtalk_signature(timestamp, sign):
            logger.error("钉钉签名验证失败")
            raise HTTPException(status_code=403, detail="Invalid Signature")

    payload = await request.json()
    logger.info(f"收到钉钉消息: {payload.get('text', {}).get('content', '')}")
    
    # 提取纯文本
    text_content = payload.get("text", {}).get("content", "").strip()
    if not text_content:
        return {"msgtype": "text", "text": {"content": "我没有收到任何文本哦。"}}

    # 将耗时任务放入后台，避免钉钉 3 秒超时
    background_tasks.add_task(async_process_dingtalk, payload, text_content)

    return {"msgtype": "empty"}


async def async_process_dingtalk(payload: Dict[str, Any], text_content: str):
    """后台异步处理钉钉消息并回调 sessionWebhook"""
    try:
        intent = await parse_intent(text_content)

        if intent.intent_type == "import":
            result_msg = await _handle_import(text_content)
        else:
            result_msg = await _handle_query(intent)
            
        session_webhook = payload.get("sessionWebhook")
        if session_webhook:
            async with httpx.AsyncClient() as client:
                await client.post(session_webhook, json=result_msg)
        else:
            logger.warning("未找到 sessionWebhook，无法异步回复钉钉消息")
    except Exception as e:
        logger.error(f"异步处理钉钉消息失败: {e}")
        session_webhook = payload.get("sessionWebhook")
        if session_webhook:
            error_msg = {"msgtype": "text", "text": {"content": f"❌ 处理失败: {e}"}}
            async with httpx.AsyncClient() as client:
                await client.post(session_webhook, json=error_msg)

# ─────────────────────────────────────────────
# 查价流程
# ─────────────────────────────────────────────

async def _handle_query(intent) -> Dict[str, Any]:
    """处理查价类意图（intent 已解析完毕）。"""
    if not intent.is_valid:
        return {"msgtype": "text", "text": {"content": intent.message}}

    # 从数据库拉取数据
    session_factory = get_async_session()
    async with session_factory() as db:
        # 获取港口全称以便展示
        pol_port = (await db.execute(select(Port).where(Port.code == intent.pol_code))).scalar_one_or_none()
        pod_port = (await db.execute(select(Port).where(Port.code == intent.pod_code))).scalar_one_or_none()
        pol_display = pol_port.name_en if pol_port else intent.pol_code
        pod_display = pod_port.name_en if pod_port else intent.pod_code

        stmt = (
            select(OceanRate)
            .where(
                OceanRate.pol_code == intent.pol_code,
                OceanRate.pod_code == intent.pod_code
            )
        )
        result = await db.execute(stmt)
        rates = result.scalars().all()

    # 调用雷达服务
    radar_res = await get_route_recommendations(
        list(rates), 
        intent.container_type, 
        intent.pol_code, 
        intent.pod_code
    )
    
    recs = radar_res.get("recommendations", [])
    risk = radar_res.get("risk_insight", "")
    
    # 构造钉钉 Markdown 回复
    if not recs:
        md_text = f"❌ 未找到从 **{pol_display}** 到 **{pod_display}** 的 {intent.container_type} 报价。"
    else:
        lines = [
            f"### {pol_display} ➔ {pod_display}",
            f"**箱型**: {intent.container_type} | **检索结果**: {min(len(recs), 5)} 条",
            ""
        ]
        
        lines.append(f"| 船公司 | {intent.container_type} | 直达/中转 | 中转港 | 船期 | 航程(天) | 有效期 | 备注 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        
        for i, r in enumerate(recs[:5]):
            carrier = r.get('carrier') or "-"
            price = f"${r.get('price')}" if r.get('price') else "-"
            route_type = r.get('route_type') or "-"
            transit_port = r.get('transit_port') or "-"
            etd_weekday = r.get('etd_weekday') or "-"
            tt_days = str(r.get('tt_days')) if r.get('tt_days') else "-"
            validity_period = r.get('validity_period') or "-"
            remarks = r.get('remarks') or "-"
            
            lines.append(f"| {carrier} | {price} | {route_type} | {transit_port} | {etd_weekday} | {tt_days} | {validity_period} | {remarks} |")

        lines.append("")
            
        lines.append(f"**🤖 风险建议:**")
        lines.append(f"> {risk}")
        
        md_text = "\n".join(lines)

    return {
        "msgtype": "markdown",
        "markdown": {
            "title": "FFORS 智能雷达报告",
            "text": md_text
        },
        "at": {"isAtAll": False}
    }


# ─────────────────────────────────────────────
# 录入流程
# ─────────────────────────────────────────────

async def _handle_import(text_content: str) -> Dict[str, Any]:
    """处理运价录入类意图。"""
    from app.services.text_importer import import_rates_from_text

    logger.info(f"[录入模式] 开始 AI 解析文本...")
    result_text = await import_rates_from_text(text_content)
    logger.info(f"[录入模式] 结果: {result_text}")

    return {
        "msgtype": "markdown",
        "markdown": {
            "title": "FFORS 运价录入",
            "text": result_text
        },
        "at": {"isAtAll": False}
    }


# ─────────────────────────────────────────────
# 企业微信交互接口 (预留)
# ─────────────────────────────────────────────

@router.get("/wecom/receive", summary="企业微信 URL 验证预留")
async def verify_wecom_url(request: Request):
    """企业微信后台填入回调 URL 时的 GET 握手验证接口 (需实现加解密)"""
    return "wecom_reserved"

@router.post("/wecom/receive", summary="企业微信消息回调预留")
async def receive_wecom_message(request: Request):
    """企业微信群聊消息的接收入口 (需解密 XML)"""
    return "success"
