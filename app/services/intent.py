"""
FFORS 意图识别服务 (Intent Service)
处理来自机器人的自然语言消息，通过大模型提取核心查询参数（起运港、目的港、箱型）。
支持 MiniMax / Gemini 双引擎自动降级切换。
"""

import json
import re
import httpx
from typing import Optional
from pydantic import BaseModel

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("ffors.services.intent")


class RadarIntent(BaseModel):
    pol_code: Optional[str] = None
    pod_code: Optional[str] = None
    container_type: str = "40HQ"  # 默认 40HQ
    is_valid: bool = False
    message: str = "未识别到起运港或目的港。"


# 统一 Prompt，供两个引擎复用
INTENT_PROMPT = """你是一个航运业务意图解析助手。
请从以下用户输入中提取起运港 (POL)、目的港 (POD) 以及箱型 (20GP, 40GP, 40HQ)。
如果用户提到港口中文名（如"上海"、"鹿特丹"、"汉堡"），请务必转换为标准的五字码港口代码（如 "CNSHA", "NLRTM", "DEHAM"）。
如果用户未指定箱型，默认为 "40HQ"。

你必须只返回一个合法的 JSON，不要返回其他任何内容。格式如下：
{{
  "pol_code": "CNSHA",
  "pod_code": "NLRTM",
  "container_type": "40HQ",
  "is_valid": true,
  "message": "识别成功"
}}
如果未能识别出 pol_code 或 pod_code，请将 is_valid 置为 false，并在 message 中说明原因。

用户输入：
"{user_text}"
"""


def _extract_intent_from_text(reply_text: str) -> Optional[RadarIntent]:
    """从大模型回复文本中提取 JSON 并解析为 RadarIntent。"""
    json_match = re.search(r"\{.*\}", reply_text, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group())
            return RadarIntent(**parsed)
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"JSON 解析失败: {e}, 原文: {reply_text}")
    return None


# ─────────────────────────────────────────────
# 引擎 1: MiniMax (chatcompletion_pro)
# ─────────────────────────────────────────────

async def _call_minimax(prompt: str) -> Optional[str]:
    """调用 MiniMax chatcompletion_pro API，返回模型回复文本。"""
    api_key = settings.minimax_api_key
    if not api_key:
        return None

    url = f"{settings.minimax_base_url}/text/chatcompletion_pro?GroupId={settings.minimax_group_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "MiniMax-Text-01",
        "bot_setting": [{
            "bot_name": "IntentParser",
            "content": "你是一个航运业务意图解析助手，只返回合法JSON，不要附加任何解释。",
        }],
        "messages": [{"sender_type": "USER", "sender_name": "User", "text": prompt}],
        "reply_constraints": {"sender_type": "BOT", "sender_name": "IntentParser"},
        "temperature": 0.1,
    }

    proxies = settings.https_proxy or settings.http_proxy or None
    async with httpx.AsyncClient(proxy=proxies, timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        # 检查 API 级别错误（余额不足、参数缺失等）
        base_resp = data.get("base_resp", {})
        status_code = base_resp.get("status_code", 0)
        if status_code != 0:
            raise Exception(f"MiniMax API error ({status_code}): {base_resp.get('status_msg', 'unknown')}")

        if "choices" in data and data["choices"]:
            return data["choices"][0]["messages"][0]["text"].strip()
    return None


# ─────────────────────────────────────────────
# 引擎 2: Gemini (降级备选)
# ─────────────────────────────────────────────

async def _call_gemini(prompt: str) -> Optional[str]:
    """调用 Google Gemini API，返回模型回复文本。"""
    api_key = settings.gemini_api_key
    if not api_key:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
        },
    }

    proxies = settings.https_proxy or settings.http_proxy or None
    async with httpx.AsyncClient(proxy=proxies, timeout=15.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

        if "candidates" in data and data["candidates"]:
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            if parts:
                return parts[0].get("text", "").strip()
    return None


# ─────────────────────────────────────────────
# 主入口：自动降级调度
# ─────────────────────────────────────────────

async def parse_intent_for_radar(user_text: str) -> RadarIntent:
    """
    解析用户输入的自然语言，提取起运港、目的港与箱型。
    优先使用 MiniMax，失败时自动降级到 Gemini。
    """
    prompt = INTENT_PROMPT.format(user_text=user_text)

    # --- 引擎 1: MiniMax ---
    try:
        reply = await _call_minimax(prompt)
        if reply:
            logger.info(f"[MiniMax] 回复: {reply}")
            intent = _extract_intent_from_text(reply)
            if intent:
                return intent
            logger.warning("[MiniMax] 返回了非预期格式，降级到 Gemini...")
    except Exception as e:
        logger.warning(f"[MiniMax] 调用失败 ({e})，降级到 Gemini...")

    # --- 引擎 2: Gemini (降级) ---
    try:
        reply = await _call_gemini(prompt)
        if reply:
            logger.info(f"[Gemini] 回复: {reply}")
            intent = _extract_intent_from_text(reply)
            if intent:
                return intent
            logger.error(f"[Gemini] 返回了非预期格式: {reply}")
    except Exception as e:
        logger.error(f"[Gemini] 调用也失败: {e}")

    return RadarIntent(message="所有 AI 引擎均无法解析，请稍后再试。")
