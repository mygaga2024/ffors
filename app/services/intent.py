"""
FFORS 意图识别服务 (Intent Service)
处理来自机器人的自然语言消息，通过大模型提取核心查询参数（起运港、目的港、箱型）。
支持 MiniMax / Gemini 双引擎自动降级切换。
一次调用同时完成 意图分类 + 参数提取，避免多次 AI 调用导致钉钉超时。
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
    intent_type: str  # query, import
    pol_code: Optional[str] = None
    pod_code: Optional[str] = None
    container_type: Optional[str] = "40GP"
    carrier: Optional[str] = None  # 指定船公司
    is_valid: bool = True
    message: str = "未识别到起运港或目的港。"


# 统一 Prompt：一次调用完成分类 + 解析
INTENT_PROMPT = """你是一个航运业务智能助手，需要完成两项任务：

**任务1：意图分类**
判断用户消息属于哪种意图：
- "query"：用户想查询报价、比价、查船期、问运费（如"上海到汉堡20GP报价"、"最近去鹿特丹有什么便宜的船"）
- "import"：用户想录入、保存报价数据到系统（如"帮我存一下报价"），或者直接发了一段包含港口、船公司、价格的报价数据

- 从用户输入中提取起运港 (POL)、目的港 (POD)、箱型 (20GP, 40GP, 40HQ) 以及指定的船公司 (Carrier)
- 行业术语映射规则：
  * "大柜"、"大箱"、"40GP" ➔ 映射为 "40GP"
  * "高箱"、"高柜"、"40HQ" ➔ 映射为 "40HQ"
  * "小柜"、"小箱"、"20GP" ➔ 映射为 "20GP"
- 船公司映射规则：将中文名转换为标准代码（如 "马士基"→"MSK" 或 "MAERSK"、"中远海运"→"COSCO"、"地中海"→"MSC"、"长荣"→"EMC"）
- 港口中文名需转换为标准五字码（如"上海"→"CNSHA"、"鹿特丹"→"NLRTM"、"汉堡"→"DEHAM"）
- 如果用户未指定起运港，默认为 "CNSHA"（上海）
- 如果用户未指定箱型，默认为 "40GP" (大柜)

你必须只返回一个合法的 JSON，不要返回其他任何内容。格式如下：
{{
  "intent_type": "query",
  "pol_code": "CNSHA",
  "pod_code": "NLRTM",
  "container_type": "40HQ",
  "carrier": "MSK",
  "is_valid": true,
  "message": "识别成功"
}}

规则：
- 如果是 import 意图：intent_type 设为 "import"，is_valid 设为 true，其他字段可为 null
- 如果是 query 意图但未能识别出 pol_code 或 pod_code：is_valid 设为 false，message 中说明原因
- 如果是 query 意图且识别成功：is_valid 设为 true

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
# 引擎 2: DeepSeek (新补位，高稳定)
# ─────────────────────────────────────────────

async def _call_deepseek(prompt: str) -> Optional[str]:
    """调用 DeepSeek API，返回模型回复文本。"""
    api_key = settings.deepseek_api_key
    if not api_key:
        return None

    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "你是一个航运业务意图解析助手，只返回合法JSON，不要附加任何解释。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
    }

    proxies = settings.https_proxy or settings.http_proxy or None
    async with httpx.AsyncClient(proxy=proxies, timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"].strip()
    return None


# ─────────────────────────────────────────────
# 引擎 3: Gemini (降级备选)
# ─────────────────────────────────────────────

async def _call_gemini(prompt: str) -> Optional[str]:
    """调用 Google Gemini API，支持多版本自动回退。"""
    api_key = settings.gemini_api_key
    if not api_key:
        return None

    # 按优先级尝试不同版本
    models = ["gemini-3.1-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
    
    for model_name in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1},
        }

        try:
            proxies = settings.https_proxy or settings.http_proxy or None
            async with httpx.AsyncClient(proxy=proxies, timeout=15.0) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    data = response.json()
                    if "candidates" in data and data["candidates"]:
                        parts = data["candidates"][0].get("content", {}).get("parts", [])
                        if parts:
                            return parts[0].get("text", "").strip()
                logger.warning(f"[Gemini] 模型 {model_name} 请求失败 (Status: {response.status_code})，尝试下一个...")
        except Exception as e:
            logger.warning(f"[Gemini] 模型 {model_name} 异常: {e}")
            
    return None


# ─────────────────────────────────────────────
# 主入口：一次调用完成分类+解析
# ─────────────────────────────────────────────

async def parse_intent(user_text: str) -> RadarIntent:
    """
    解析用户输入的自然语言，一次 AI 调用同时完成：
    1. 意图分类（query / import）
    2. 查价参数提取（pol_code, pod_code, container_type）
    优先使用 MiniMax，失败时自动降级到 Gemini。
    """
    prompt = INTENT_PROMPT.format(user_text=user_text)

    # --- 引擎 1: DeepSeek (V4 Flagship) ---
    try:
        reply = await _call_deepseek(prompt)
        if reply:
            intent = _extract_intent_from_text(reply)
            if intent:
                return intent
    except Exception as e:
        logger.warning(f"[DeepSeek] 调用失败 ({e})，尝试 MiniMax...")

    # --- 引擎 2: MiniMax ---
    try:
        reply = await _call_minimax(prompt)
        if reply:
            intent = _extract_intent_from_text(reply)
            if intent:
                return intent
    except Exception as e:
        logger.warning(f"[MiniMax] 调用失败 ({e})，尝试 Gemini...")

    # --- 引擎 3: Gemini (降级) ---
    try:
        reply = await _call_gemini(prompt)
        if reply:
            intent = _extract_intent_from_text(reply)
            if intent:
                return intent
    except Exception as e:
        logger.error(f"[Gemini] 调用也失败: {e}")

    return RadarIntent(message="所有 AI 引擎均无法解析，请稍后再试。")


# 保留旧函数名的兼容别名
async def parse_intent_for_radar(user_text: str) -> RadarIntent:
    return await parse_intent(user_text)
