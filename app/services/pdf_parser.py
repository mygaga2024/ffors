"""
FFORS PDF 视觉/文本解析服务 (基于 MiniMax)
负责自动分页、提取文本/图像，并并行调用大模型解析为结构化数据。
遵循 DEVELOPMENT_PROTOCOL.md：
  - §1 最小干预：严格限定输出为 JSON 数组，以便复用 Excel 导入流程。
  - §5 代理感知：调用 MiniMax API 必须带入代理配置。
"""

import asyncio
import json
import re
from io import BytesIO
from typing import Any

import fitz  # PyMuPDF
import httpx
import pandas as pd

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger("ffors.services.pdf_parser")

# 限制单次并发页数，防止耗尽 API QPS 或系统内存
MAX_CONCURRENT_PAGES = 5


def _extract_pages_from_pdf(file_bytes: bytes) -> list[str]:
    """
    使用 PyMuPDF 对 PDF 进行分页，并提取每页的文本内容。
    如果是全图扫描件，当前基于纯文本的模型会提空，这里预留了扩展为视觉请求的空间。
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages_text = []

    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        text = page.get_text("text").strip()
        # 如果当前页文本少于 20 个字符，可能是一张纯图片
        if len(text) < 20:
            logger.warning(f"第 {page_num + 1} 页提取文本过少，可能包含扫描图像。")
            # 备注：如需支持纯扫描件，可在此将 page 转为 image base64 喂给 MiniMax 视觉模型。
            # 目前按要求优先并行处理，尽量使用文本。
        
        # 即使文本很少，也传入让模型判断
        pages_text.append(text)

    doc.close()
    return pages_text


def _build_prompt(page_text: str) -> str:
    """
    构造强制要求返回标准 JSON 数组的 Prompt。
    要求字段与 Excel Ingestion 时的 COLUMN_MAP 对齐。
    """
    return f"""你是一个专业的海运报价单数据录入员。
请从以下包含杂乱文本的货代报价单页面中，提取出所有海运报价记录。

要求：
1. 必须且只能输出一个合法的 JSON 数组结构。不要包含任何 Markdown 标记（如 ```json）。
2. JSON 数组中的每个对象必须包含以下字段：
    - "pol_code": 起运港代码或名称（字符串，必填）
    - "pod_code": 目的港代码或名称（字符串，必填）
    - "carrier": 船公司名称（字符串，必填）
    - "price_20gp": 20GP 价格（数字，没有则返回 null）
    - "price_40gp": 40GP 价格（数字，没有则返回 null）
    - "price_40hq": 40HQ 价格（数字，没有则返回 null）
    - "currency": 币种（默认为 "USD"）
    - "tt_days": 航程时效天数（整数，如 15、30。极其重要！没有则返回 null）
    - "remarks": 附加费或备注信息（字符串，没有则返回 ""）

如果该页没有任何报价数据，请返回一个空数组 `[]`。

以下是需要提取的页面文本：
---
{page_text}
---
"""


async def _parse_single_page(client: httpx.AsyncClient, page_num: int, page_text: str) -> list[dict[str, Any]]:
    """
    调用 MiniMax 接口解析单页文本。
    """
    if not page_text.strip():
        return []

    api_key = settings.minimax_api_key
    base_url = settings.minimax_base_url
    group_id = settings.minimax_group_id

    url = f"{base_url}/text/chatcompletion_pro?GroupId={group_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    prompt = _build_prompt(page_text)
    
    payload = {
        "model": "MiniMax-Text-01",
        "messages": [
            {
                "sender_type": "USER",
                "sender_name": "DataParser",
                "text": prompt,
            }
        ],
        "reply_constraints": {"sender_type": "BOT", "sender_name": "Analyst"},
        "tokens_to_generate": 1024,
        "temperature": 0.1,  # 使用低温度保证 JSON 提取稳定性
    }

    try:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()

        reply_text = ""
        choices = result.get("choices", [])
        if choices:
            messages = choices[0].get("messages", [])
            if messages:
                reply_text = messages[0].get("text", "")

        if not reply_text:
            return []

        # 正则清理，提取最外层的 `[` 到 `]` 之间的内容
        json_match = re.search(r"\[.*\]", reply_text, re.DOTALL)
        if json_match:
            try:
                parsed_data = json.loads(json_match.group())
                if isinstance(parsed_data, list):
                    return parsed_data
            except json.JSONDecodeError:
                pass
        
        logger.warning(f"第 {page_num + 1} 页 AI 解析 JSON 失败: {reply_text[:100]}")
        return []

    except Exception as e:
        logger.error(f"第 {page_num + 1} 页 AI 调用异常: {e}")
        return []


async def parse_pdf_to_dataframe(file_bytes: bytes) -> pd.DataFrame:
    """
    入口函数：
    1. PyMuPDF 分页。
    2. 并发调用 MiniMax 解析每页。
    3. 组装为 Pandas DataFrame (与 Excel 导入结构对齐)。
    """
    if not settings.minimax_api_key:
        raise ValueError("系统未配置 MINIMAX_API_KEY，无法解析 PDF。")

    logger.info("开始解析 PDF 文件...")
    pages_text = _extract_pages_from_pdf(file_bytes)
    total_pages = len(pages_text)
    logger.info(f"PDF 分页完成，共 {total_pages} 页。开始并行请求大模型...")

    all_rates = []
    proxies = settings.http_proxy or None

    # 创建一个持久的 httpx.AsyncClient 用于复用连接池
    async with httpx.AsyncClient(proxy=proxies, timeout=60.0) as client:
        # 使用 Semaphore 控制最大并发页数
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

        async def _bounded_parse(idx: int, text: str):
            async with semaphore:
                return await _parse_single_page(client, idx, text)

        # 构造并发任务列表
        tasks = [
            _bounded_parse(idx, text)
            for idx, text in enumerate(pages_text)
        ]

        # 并行执行
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for page_num, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(f"第 {page_num + 1} 页执行异常: {res}")
            elif isinstance(res, list):
                all_rates.extend(res)

    if not all_rates:
        raise ValueError("大模型未能从该 PDF 中提取出任何有效的报价数据。")

    # 转换为 DataFrame
    df = pd.DataFrame(all_rates)
    logger.info(f"PDF AI 解析完成，成功提取 {len(df)} 条记录。")
    return df
