"""
FFORS 文本运价导入服务 (Text Importer)
通过 AI 大模型从自由文本/表格中提取结构化运价数据，并调用现有入库逻辑批量写入。
"""

import json
import re
import httpx
import pandas as pd
from typing import Optional

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("ffors.services.text_importer")

EXTRACT_PROMPT = """你是一个顶级的货代海运报价解析专家。
请从以下用户输入中提取所有运价信息，并整理为标准 JSON 数组。

### 核心解析规则：
1. **地名硬映射 (必须遵守)**：
   - 上海 -> CNSHA, 洛杉矶 -> USLAX, 西雅图 -> USSEA, 纽约 -> USNYC, 鹿特丹 -> NLRTM, 汉堡 -> DEHAM
2. **识别层级结构**：
   - 文本可能具有层级：起运港(POL) -> 目的港(POD) -> 船公司明细。
   - 如果某行提到"上海出"，后续明细默认 POL 均为 "CNSHA"。
3. **箱型识别**：
   - `20GP`/`小柜` -> price_20gp；`40GP`/`大柜` -> price_40gp；`40HQ`/`高柜`/`高箱`/`40'HQ` -> price_40hq。
4. **关键转换**：
   - 长荣 -> EMC, 马士基 -> MSK, 中远 -> COSCO, 地中海 -> MSC, 达飞 -> CMA, 赫伯罗特 -> HPL, 万海 -> WHL。
   - "直达" -> route_type: "DIRECT", "中转" -> route_type: "TRANSIT"。
5. **日期处理**：
   - "船期周 5" -> 请在 etd 字段推算一个近期日期（如 2026-05-01），并在 remarks 记录。

### 待解析文本：
"{user_text}"

你必须只返回 JSON 数组 [{{...}}]，严禁输出任何解释文字或代码块标签。"""


async def extract_rates_from_text(user_text: str) -> Optional[list[dict]]:
    """
    调用 AI 模型从自由文本中提取运价数据。
    优先级：MiniMax -> DeepSeek -> Gemini (3.1 -> 2.5 -> 1.5)
    """
    prompt = EXTRACT_PROMPT.format(user_text=user_text)

    # --- 引擎 1: MiniMax ---
    try:
        result = await _call_minimax_extract(prompt)
        if result: return result
    except Exception as e:
        logger.warning(f"[MiniMax] 提取失败: {e}")

    # --- 引擎 2: DeepSeek ---
    try:
        result = await _call_deepseek_extract(prompt)
        if result: return result
    except Exception as e:
        logger.warning(f"[DeepSeek] 提取失败: {e}")

    # --- 引擎 3: Gemini (多版本回退) ---
    for model_version in ["gemini-1.5-pro", "gemini-1.5-flash"]:
        try:
            result = await _call_gemini_extract(prompt, model_version)
            if result: return result
        except Exception as e:
            logger.warning(f"[Gemini-{model_version}] 提取失败: {e}")

    return None


async def _call_minimax_extract(prompt: str) -> Optional[list[dict]]:
    """MiniMax 提取"""
    api_key = settings.minimax_api_key
    if not api_key:
        return None

    url = f"{settings.minimax_base_url}/text/chatcompletion_pro?GroupId={settings.minimax_group_id}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "MiniMax-Text-01",
        "bot_setting": [{"bot_name": "RateExtractor", "content": "你是运价数据提取助手，只返回JSON数组。"}],
        "messages": [{"sender_type": "USER", "sender_name": "User", "text": prompt}],
        "reply_constraints": {"sender_type": "BOT", "sender_name": "RateExtractor"},
        "temperature": 0.1,
    }

    proxies = settings.https_proxy or settings.http_proxy or None
    async with httpx.AsyncClient(proxy=proxies, timeout=20.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        base_resp = data.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise Exception(f"MiniMax error: {base_resp.get('status_msg')}")

        if "choices" in data and data["choices"]:
            reply = data["choices"][0]["messages"][0]["text"].strip()
            return _parse_json_array(reply)
    return None


async def _call_deepseek_extract(prompt: str) -> Optional[list[dict]]:
    """DeepSeek 提取"""
    api_key = settings.deepseek_api_key
    if not api_key: return None

    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是一个运价提取助手，只返回 JSON 数组。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
    }

    proxies = settings.http_proxy or None
    async with httpx.AsyncClient(proxy=proxies, timeout=30.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        if "choices" in data and data["choices"]:
            reply = data["choices"][0]["message"]["content"].strip()
            return _parse_json_array(reply)
    return None


async def _call_gemini_extract(prompt: str, model: str = "gemini-1.5-flash") -> Optional[list[dict]]:
    """Gemini 提取"""
    api_key = settings.gemini_api_key
    if not api_key:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }

    proxies = settings.https_proxy or settings.http_proxy or None
    async with httpx.AsyncClient(proxy=proxies, timeout=20.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

        if "candidates" in data and data["candidates"]:
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            if parts:
                reply = parts[0].get("text", "").strip()
                return _parse_json_array(reply)
    return None


def _parse_json_array(text: str) -> Optional[list[dict]]:
    """从 AI 回复中提取 JSON 数组。"""
    # 尝试提取 JSON 数组
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list) and len(result) > 0:
                logger.info(f"AI 成功提取 {len(result)} 条运价数据")
                return result
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}")
    return None


async def import_rates_from_text(user_text: str) -> str:
    """
    完整流程：AI 提取 → 构造 DataFrame → 调用批量导入 → 返回结果摘要。
    """
    from app.models.base import get_async_session
    from app.services.ingestion import batch_import_rates

    # 1. AI 提取
    rates_data = await extract_rates_from_text(user_text)
    if not rates_data:
        return "❌ 未能从文本中提取到有效的运价数据，请检查输入格式。"

    # 2. 构造 DataFrame（列名需匹配 ingestion.py 的 COLUMN_MAP）
    rows = []
    for r in rates_data:
        rows.append({
            "POL": r.get("pol_code", ""),
            "POD": r.get("pod_code", ""),
            "Carrier": r.get("carrier", ""),
            "20GP": r.get("price_20gp"),
            "40GP": r.get("price_40gp"),
            "40HQ": r.get("price_40hq"),
            "Currency": r.get("currency", "USD"),
            "ETD": r.get("etd"),
            "TT(Days)": r.get("tt_days"),
            "Valid From": r.get("valid_from"),
            "Valid To": r.get("valid_to"),
            "Remarks": r.get("remarks", ""),
        })

    df = pd.DataFrame(rows)

    # 3. 调用现有批量导入
    session_factory = get_async_session()
    async with session_factory() as db:
        result = await batch_import_rates(df, source_file="钉钉对话导入", db=db)

    # 4. 构造友好回复
    if result.success == 0:
        error_detail = result.errors[0].reason if result.errors else "未知错误"
        return f"❌ 导入失败：{error_detail}"

    # 成功摘要
    lines = [f"✅ 成功录入 **{result.success}** 条报价（共 {result.total} 条）"]
    for r in rates_data[:5]:  # 最多展示 5 条
        pol = r.get("pol_code", "?")
        pod = r.get("pod_code", "?")
        carrier = r.get("carrier", "?")
        p20 = r.get("price_20gp")
        p40 = r.get("price_40gp")
        p40h = r.get("price_40hq")
        prices = []
        if p20: prices.append(f"20GP/${p20}")
        if p40: prices.append(f"40GP/${p40}")
        if p40h: prices.append(f"40HQ/${p40h}")
        price_str = " | ".join(prices) if prices else "价格未知"
        lines.append(f"- {pol}→{pod} **{carrier}** {price_str}")

    if result.failed > 0:
        lines.append(f"\n⚠️ {result.failed} 条导入失败")

    return "\n".join(lines)
