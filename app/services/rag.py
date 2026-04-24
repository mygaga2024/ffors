"""
FFORS RAG (检索增强生成) 核心服务
基于 ChromaDB 和 MiniMax 向量模型，提供知识入库与语义检索能力。
"""

import os
from typing import Optional, List

import chromadb
import httpx
from chromadb import Documents, EmbeddingFunction, Embeddings
from sqlalchemy import select

from app.config import settings
from app.models.base import get_async_session
from app.models.news import MaritimeNews
from app.utils.logger import get_logger

logger = get_logger("ffors.services.rag")

# 向量数据库存储路径 (挂载在 Docker data 目录)
CHROMA_DB_PATH = os.environ.get("DATA_DIR", "/app/data") + "/chroma"


class MiniMaxEmbeddingFunction(EmbeddingFunction):
    """自定义 ChromaDB 嵌入函数：对接 MiniMax 向量模型"""
    
    def __call__(self, input: Documents) -> Embeddings:
        if not settings.minimax_api_key:
            logger.warning("未配置 MINIMAX_API_KEY，无法生成文本向量。")
            # 为防止 ChromaDB 报错，返回假的零向量数组 (仅限调试)
            return [[0.0] * 1536 for _ in input]
            
        api_key = settings.minimax_api_key
        base_url = settings.minimax_base_url
        group_id = settings.minimax_group_id
        
        # MiniMax embedding Endpoint
        # 注意：这里使用同步 HTTP 请求，因为 ChromaDB 内置机制要求 EmbeddingFunction 同步返回
        url = f"{base_url}/embeddings?GroupId={group_id}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "model": "embo-01",  # MiniMax 文本向量模型
            "texts": input
        }
        
        proxies = settings.http_proxy or None
        
        try:
            with httpx.Client(proxy=proxies, timeout=15.0) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                
                # 提取返回的向量组
                embeddings = []
                vectors = data.get("vectors", [])
                for vec in vectors:
                    embeddings.append(vec)
                return embeddings
        except Exception as e:
            logger.error(f"MiniMax 向量化请求失败: {e}")
            raise


# 全局单例 Chroma 客户端
_chroma_client = None

def get_chroma_collection():
    """获取/初始化 ChromaDB 集合"""
    global _chroma_client
    if _chroma_client is None:
        os.makedirs(CHROMA_DB_PATH, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        
    return _chroma_client.get_or_create_collection(
        name="maritime_knowledge",
        embedding_function=MiniMaxEmbeddingFunction()
    )


async def ingest_news_to_vector_db():
    """
    将关系型数据库(PostgreSQL)中新增的航运新闻同步至向量数据库(ChromaDB)。
    由定时任务调度执行。
    """
    logger.info("开始同步航运新闻至 RAG 向量数据库...")
    
    collection = get_chroma_collection()
    # 获取 Chroma 中已存在的记录总数
    existing_count = collection.count()
    
    session_factory = get_async_session()
    async with session_factory() as db:
        # 为了避免每次全量灌注，根据已有的数量作为 Offset (这是一种简化的增量同步策略)
        # 生产环境中可以给新闻表增加 `is_embedded` 字段，这里为了遵循“最小干预”使用 offset 策略。
        stmt = select(MaritimeNews).order_by(MaritimeNews.id.asc()).offset(existing_count)
        result = await db.execute(stmt)
        new_news = result.scalars().all()
        
    if not new_news:
        logger.info("向量数据库已是最新，无需同步。")
        return
        
    ids = []
    documents = []
    metadatas = []
    
    for news in new_news:
        ids.append(f"news_{news.id}")
        # 将标题和摘要合并作为向量文档
        text = f"【{news.title}】\n{news.content if news.content else news.summary}"
        documents.append(text)
        metadatas.append({
            "source": news.source or "unknown",
            "link": news.link,
            "published_at": news.published_at.isoformat() if news.published_at else "",
            "type": "news"
        })
        
    # ChromaDB 支持批量 upsert
    try:
        # 如果一次性同步数据量过大，可能需要进行 Batch 分割，此处假设定时任务执行频繁，每次数据量不大
        collection.upsert(
            ids=ids,
            documents=documents,
            metadatas=metadatas
        )
        logger.info(f"成功将 {len(ids)} 条新资讯灌注至 RAG 向量库。")
    except Exception as e:
        logger.error(f"灌注 RAG 向量库失败: {e}")


def search_knowledge(query: str, top_k: int = 3) -> str:
    """
    检索相关知识。供“比价雷达”或“机器人”获取外部上下文。
    """
    try:
        collection = get_chroma_collection()
        results = collection.query(
            query_texts=[query],
            n_results=top_k
        )
        
        context_parts = []
        if results and results.get("documents") and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i]
                context_parts.append(f"来源: {meta.get('source')} - {doc[:200]}...")
                
        return "\n\n".join(context_parts)
    except Exception as e:
        logger.error(f"检索知识库失败: {e}")
        return ""
