"""
FFORS 意图识别服务 (Intent Service)
处理来自机器人的自然语言消息，通过 MiniMax 大模型提取核心查询参数（起运港、目的港、箱型）。
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


async def parse_intent_for_radar(user_text: str) -> RadarIntent:
    """
    解析用户输入的自然语言，提取起步港、目的港与箱型。
    """
    api_key = settings.minimax_api_key
    if not api_key:
        logger.error("未配置 MINIMAX_API_KEY，无法解析用户意图。")
        return RadarIntent(message="系统暂未配置 AI 模型，无法理解指令。")

    url = f"{settings.minimax_base_url}/text/chatcompletion_pro?GroupId={settings.minimax_group_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 构建严格的 Prompt 强制返回 JSON
    prompt = f"""你是一个航运业务意图解析助手。
请从以下用户输入中提取起步港 (POL)、目的港 (POD) 以及箱型 (20GP, 40GP, 40HQ)。
如果用户提到港口中文名（如“上海”、“鹿特丹”、“汉堡”），请务必转换为标准的五字五字港口代码（如 "CNSHA", "NLRTM", "DEHAM"）。
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

    payload = {
        "model": "MiniMax-Text-01",
        "messages": [{"sender_type": "USER", "sender_name": "User", "text": prompt}],
        "reply_constraints": {"sender_type": "BOT", "sender_name": "IntentParser"},
        "temperature": 0.1,  # 保证稳定输出 JSON
    }

    try:
        proxies = settings.http_proxy or None
        async with httpx.AsyncClient(proxy=proxies, timeout=10.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            
            if "choices" in data and data["choices"]:
                reply_text = data["choices"][0]["messages"][0]["text"].strip()
                
                # 提取 JSON 块
                json_match = re.search(r"\{.*\}", reply_text, re.DOTALL)
                if json_match:
                    try:
                        parsed = json.loads(json_match.group())
                        return RadarIntent(**parsed)
                    except json.JSONDecodeError:
                        pass
                        
                logger.error(f"意图识别返回了非预期的格式: {reply_text}")
    except Exception as e:
        logger.error(f"调用意图解析接口异常: {e}")
        return RadarIntent(message=f"大模型解析异常: {e}")

    return RadarIntent(message="无法从大模型获取有效的解析结果。")
