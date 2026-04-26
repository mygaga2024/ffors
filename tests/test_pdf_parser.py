"""
FFORS - PDF 解析器测试套件 (Mock E2E)
用于验证在没有真实 PDF 文件和真实 MiniMax API Key 的情况下，
数据流转、正则清洗和 DataFrame 转换的健壮性。
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from httpx import Response

from app.services.pdf_parser import parse_pdf_to_dataframe, _parse_single_page
from app.config import settings

# ─────────────────────────────────────────────
# 测试夹具 (Fixtures)
# ─────────────────────────────────────────────

@pytest.fixture
def mock_settings():
    """Mock 全局设置，防止缺少 API Key 报错"""
    with patch("app.services.pdf_parser.settings") as mock_set:
        mock_set.minimax_api_key = "mock_key_123"
        mock_set.minimax_base_url = "https://mock.api"
        mock_set.minimax_group_id = "mock_group"
        mock_set.http_proxy = None
        yield mock_set


@pytest.fixture
def mock_fitz():
    """Mock PyMuPDF (fitz) 以免需要真实的物理 PDF 文件"""
    with patch("app.services.pdf_parser.fitz.open") as mock_open:
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 2  # 假装这个 PDF 有两页

        # 模拟第一页提取出的文本
        mock_page_1 = MagicMock()
        mock_page_1.get_text.return_value = "Shanghai to Rotterdam COSCO 20GP: 1200 40GP: 1800"
        
        # 模拟第二页提取出的文本 (比如是一个极短的无效页)
        mock_page_2 = MagicMock()
        mock_page_2.get_text.return_value = "Page 2 Blank"

        mock_doc.load_page.side_effect = [mock_page_1, mock_page_2]
        mock_open.return_value = mock_doc
        yield mock_open


@pytest.fixture
def mock_httpx_post():
    """Mock httpx.AsyncClient.post 返回预设的大模型响应"""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        # 我们模拟 MiniMax 返回了一段带有 Markdown 标记 (```json) 的脏字符串
        # 以测试我们的正则清洗能力
        dirty_json_response = '''
Here are the rates I found:
```json
[
  {
    "pol_code": "CNSHA",
    "pod_code": "NLRTM",
    "carrier": "COSCO",
    "price_20gp": 1200,
    "price_40gp": 1800,
    "price_40hq": null,
    "currency": "USD",
    "remarks": ""
  }
]
```
'''
        # 构造 httpx.Response 对象
        mock_response = MagicMock(spec=Response)
        mock_response.json.return_value = {
            "choices": [
                {
                    "messages": [
                        {"text": dirty_json_response}
                    ]
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response
        yield mock_post


# ─────────────────────────────────────────────
# 测试用例
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_parse_single_page_dirty_json(mock_settings, mock_httpx_post):
    """
    测试点 1：底层 JSON 提取。
    验证 _parse_single_page 是否能从带有 Markdown 和闲聊废话的 AI 返回结果中，
    使用正则精准抠出合法的 JSON 数组并反序列化。
    """
    from httpx import AsyncClient
    async with AsyncClient() as client:
        result = await _parse_single_page(client, 0, "dummy text")
    
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["pol_code"] == "CNSHA"
    assert result[0]["price_20gp"] == 1200


@pytest.mark.asyncio
async def test_parse_pdf_to_dataframe_flow(mock_settings, mock_fitz, mock_httpx_post):
    """
    测试点 2：完整的数据流转 (E2E)。
    验证 parse_pdf_to_dataframe 能否串联分页、并发调用、脏数据清洗，最终输出合法的 DataFrame。
    """
    # 传入任意 dummy bytes，因为 fitz.open 被 mock 了
    dummy_pdf_bytes = b"%PDF-1.4 mock content"
    
    df = await parse_pdf_to_dataframe(dummy_pdf_bytes)
    
    # 验证是否成功返回 Pandas DataFrame
    assert isinstance(df, pd.DataFrame)
    
    # 因为 mock 的 PDF 有 2 页，我们的 mock_httpx_post 会被调用 2 次。
    # 每次返回 1 条记录，总共应该组装成 2 条记录（虽然两页返回的是同样的数据，但这里只验证框架集成）
    assert len(df) == 2
    assert "pol_code" in df.columns
    assert "price_40gp" in df.columns
    
    # 检查其中一条记录的值
    assert df.iloc[0]["carrier"] == "COSCO"
    assert df.iloc[0]["price_40gp"] == 1800
    assert pd.isna(df.iloc[0]["price_40hq"]) or df.iloc[0]["price_40hq"] is None

    # 验证 httpx post 被并发调用了 2 次
    assert mock_httpx_post.call_count == 2
