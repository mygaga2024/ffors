"""
FFORS 机器人交互接口 (Bot Webhooks)
接收来自钉钉或企业微信的 POST 消息，通过意图解析后触发比价雷达或运价录入。
"""

import base64
import hashlib
import hmac
import re
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.base import get_async_session
from app.models.rate import OceanRate
from app.services.intent import parse_intent_for_radar
from app.services.radar import get_route_recommendations
from app.utils.logger import get_logger

logger = get_logger("ffors.api.bot")
router = APIRouter(prefix="/bot", tags=["Bot Interaction"])


# ─────────────────────────────────────────────
# 意图分类：查价 vs 录入
# ─────────────────────────────────────────────

IMPORT_KEYWORDS = [
    "录入", "导入", "整理", "入库", "存入", "保存", "记录",
    "帮我存", "帮我记", "帮我录", "写入", "添加报价", "新增报价",
]


def classify_intent(text: str) -> str:
    """
    基于关键词快速判断用户意图。
    返回 'import' 或 'query'。
    """
    text_lower = text.lower().strip()
    for kw in IMPORT_KEYWORDS:
        if kw in text_lower:
            return "import"
    return "query"


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

    # ─── 意图分类 ───
    intent_type = classify_intent(text_content)

    if intent_type == "import":
        return await _handle_import(text_content)
    else:
        return await _handle_query(text_content)


# ─────────────────────────────────────────────
# 查价流程（原有逻辑）
# ─────────────────────────────────────────────

async def _handle_query(text_content: str) -> Dict[str, Any]:
    """处理查价类意图。"""
    # 1. 意图解析
    intent = await parse_intent_for_radar(text_content)
    if not intent.is_valid:
        return {"msgtype": "text", "text": {"content": intent.message}}

    # 2. 从数据库拉取数据
    session_factory = get_async_session()
    async with session_factory() as db:
        stmt = (
            select(OceanRate)
            .where(
                OceanRate.pol_code == intent.pol_code,
                OceanRate.pod_code == intent.pod_code
            )
        )
        result = await db.execute(stmt)
        rates = result.scalars().all()

    # 3. 调用雷达服务
    radar_res = await get_route_recommendations(
        list(rates), 
        intent.container_type, 
        intent.pol_code, 
        intent.pod_code
    )
    
    recs = radar_res.get("recommendations", [])
    risk = radar_res.get("risk_insight", "")
    
    # 4. 构造钉钉 Markdown 回复
    if not recs:
        md_text = f"❌ 未找到从 **{intent.pol_code}** 到 **{intent.pod_code}** 的 {intent.container_type} 报价。"
    else:
        lines = [
            f"### ⚡ FFORS 比价雷达 ({intent.pol_code} ➔ {intent.pod_code})",
            f"**箱型**: {intent.container_type} | **检索结果**: {len(recs)} 条",
            ""
        ]
        
        # 只展示前 3 名
        for i, r in enumerate(recs[:3]):
            tag_str = " ".join(r['tags']) if r['tags'] else ""
            medal = ["🥇", "🥈", "🥉"][i] if i < 3 else "🔸"
            tt_str = f"{r['tt_days']}天" if r['tt_days'] else "未知时效"
            
            lines.append(f"{medal} **{r['carrier']}** {tag_str}")
            lines.append(f"- 💰 价格: **${r['price']}** | ⏱️ 时效: {tt_str}")
            lines.append(f"- ⚖️ 综合得分: {r['total_score']} (稳定性: {r['stability_score']})")
            if r['remarks']:
                lines.append(f"- 📝 备注: {r['remarks']}")
            lines.append("")
            
        lines.append(f"**🤖 RAG 风险交叉验证锦囊:**")
        lines.append(f"> {risk}")
        
        md_text = "\n".join(lines)

    return {
        "msgtype": "markdown",
        "markdown": {
            "title": "FFORS 智能雷达报告",
            "text": md_text
        },
        "at": {
            "isAtAll": False
        }
    }


# ─────────────────────────────────────────────
# 录入流程（新增）
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
        "at": {
            "isAtAll": False
        }
    }


# ─────────────────────────────────────────────
# 企业微信交互接口 (预留)
# ─────────────────────────────────────────────

@router.get("/wecom/receive", summary="企业微信 URL 验证预留")
async def verify_wecom_url(request: Request):
    """企业微信后台填入回调 URL 时的 GET 握手验证接口 (需实现加解密)"""
    # 此处预留实现逻辑，正式接入需引入 wechatpy[cryptography] 或手动 AES
    return "wecom_reserved"

@router.post("/wecom/receive", summary="企业微信消息回调预留")
async def receive_wecom_message(request: Request):
    """企业微信群聊消息的接收入口 (需解密 XML)"""
    return "success"
