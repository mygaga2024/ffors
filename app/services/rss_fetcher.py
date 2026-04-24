"""
FFORS RSS 外部新闻流抓取服务
从多个外部源拉取最新的航运新闻，清洗去重并入库，供后续 RAG 和风险评分使用。
"""

import asyncio
from datetime import datetime, timezone
from time import mktime

import feedparser
import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.config import settings
from app.models.base import get_async_session
from app.models.news import MaritimeNews
from app.utils.logger import get_logger

logger = get_logger("ffors.services.rss")

# 默认的航运新闻 RSS 源 (可移至环境变量或数据库配置)
DEFAULT_RSS_FEEDS = [
    # 示例: Splash247 (知名全球航运媒体)
    "https://splash247.com/feed/",
    # 这里可配置更多信德海事、搜航网等垂直源
]


def clean_html(html_content: str) -> str:
    """去除 HTML 标签，保留纯文本，供 AI 更好地理解"""
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    # 去除 script 和 style 标签
    for script in soup(["script", "style"]):
        script.extract()
    text = soup.get_text(separator="\n")
    # 简单清理多余空白行
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    return "\n".join(chunk for chunk in chunks if chunk)


async def fetch_single_feed(url: str, client: httpx.AsyncClient) -> list[dict]:
    """异步抓取单个 RSS 源并解析"""
    try:
        response = await client.get(url)
        response.raise_for_status()
        
        # feedparser 在解析时需要 string，不是 bytes
        feed = feedparser.parse(response.text)
        entries = []
        
        source_title = feed.feed.get("title", "Unknown Source")
        
        for entry in feed.entries:
            # 提取发布时间
            pub_date = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub_date = datetime.fromtimestamp(mktime(entry.published_parsed), tz=timezone.utc)
            else:
                pub_date = datetime.now(timezone.utc)
                
            entries.append({
                "title": entry.get("title", "").strip(),
                "link": entry.get("link", "").strip(),
                "published_at": pub_date,
                "source": source_title,
                "summary": clean_html(entry.get("summary", "")),
                # 有些 RSS 源会将全文放在 content[0].value
                "content": clean_html(entry.content[0].value) if hasattr(entry, "content") else "",
            })
            
        return entries
    except Exception as e:
        logger.error(f"抓取 RSS 失败 [{url}]: {e}")
        return []


async def fetch_and_store_news():
    """
    抓取所有配置的 RSS 并在数据库中去重入库。
    """
    logger.info("开始抓取外部航运新闻流 (RSS)...")
    
    proxies = settings.http_proxy or None
    all_entries = []
    
    # 1. 并发拉取
    async with httpx.AsyncClient(proxy=proxies, timeout=30.0) as client:
        tasks = [fetch_single_feed(url, client) for url in DEFAULT_RSS_FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for res in results:
            if isinstance(res, list):
                all_entries.extend(res)
                
    if not all_entries:
        logger.info("本次抓取未获取到任何新闻记录。")
        return

    # 2. 去重与入库
    session_factory = get_async_session()
    async with session_factory() as db:
        new_count = 0
        for data in all_entries:
            if not data["link"]:
                continue
                
            # 根据 link 去重
            stmt = select(MaritimeNews.id).where(MaritimeNews.link == data["link"])
            result = await db.execute(stmt)
            exists = result.scalar_one_or_none()
            
            if not exists:
                news_item = MaritimeNews(
                    title=data["title"][:500],
                    link=data["link"][:1000],
                    published_at=data["published_at"],
                    source=data["source"][:100],
                    summary=data["summary"],
                    content=data["content"] or data["summary"],  # 如果没有 content 则 fallback
                )
                db.add(news_item)
                new_count += 1
                
        if new_count > 0:
            try:
                await db.commit()
                logger.info(f"新闻流抓取完成，成功新增 {new_count} 条记录。")
            except Exception as e:
                await db.rollback()
                logger.error(f"新闻入库失败: {e}")
        else:
            logger.info("抓取完成，没有发现新的新闻记录。")
