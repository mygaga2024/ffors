"""
FFORS RAG 服务 (Retrieval-Augmented Generation)
负责连接大模型 Embedding 接口，管理本地 ChromaDB 向量库，并提供检索能力。
遵守 DEVELOPMENT_PROTOCOL.md §4 隔离边界：本模块只负责向量转化和检索，不负责业务判断。
"""

import os
from typing import Optional

import chromadb
import httpx
from sqlalchemy import select

from app.config import settings
from app.models.base import get_async_session
from app.models.news import MaritimeNews
from app.utils.logger import get_logger

logger = get_logger("ffors.services.rag")

# ─────────────────────────────────────────────
# 初始化 ChromaDB
# ─────────────────────────────────────────────

# 使用本地持久化客户端，数据存放在宿主机挂载的 /data/chroma 下
CHROMA_PERSIST_DIR = os.getenv("DATA_DIR", "./data") + "/chroma"

try:
    chroma_client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
    # 创建或获取新闻向量集合 (Cosine 相似度)
    news_collection = chroma_client.get_or_create_collection(
        name="maritime_news",
        metadata={"hnsw:space": "cosine"}
    )
    logger.info(f"ChromaDB 向量库初始化成功，路径: {CHROMA_PERSIST_DIR}")
except Exception as e:
    logger.error(f"ChromaDB 初始化失败: {e}")
    news_collection = None


# ─────────────────────────────────────────────
# MiniMax Embedding API 调用
# ─────────────────────────────────────────────

async def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    调用 MiniMax Embedding 接口，将文本批量转换为向量。
    注意：为了避免过长文本导致截断，建议预先分块。
    """
    if not texts:
        return []

    api_key = settings.minimax_api_key
    base_url = settings.minimax_base_url
    group_id = settings.minimax_group_id

    if not api_key:
        logger.warning("未配置 MINIMAX_API_KEY，跳过 Embedding 转换。")
        return []

    url = f"{base_url}/embeddings?GroupId={group_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    # embo-01 是 MiniMax 推荐的通用文本向量模型
    payload = {
        "model": "embo-01",
        "texts": texts,
    }

    proxies = settings.http_proxy or None

    try:
        async with httpx.AsyncClient(proxy=proxies, timeout=20.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            
            vectors = result.get("vectors", [])
            if not vectors or len(vectors) != len(texts):
                logger.error("MiniMax Embedding 返回的向量数量不匹配或为空。")
                return []
            
            return vectors
            
    except Exception as e:
        logger.error(f"调用 MiniMax Embedding 接口异常: {e}")
        return []


# ─────────────────────────────────────────────
# 知识库入库与检索逻辑
# ─────────────────────────────────────────────

async def ingest_news_to_vector_db():
    """
    增量向量化：从 PostgreSQL 读取所有新闻，检查哪些尚未灌入 ChromaDB，
    然后调用 Embedding 接口并存入本地向量库。
    """
    if not news_collection:
        logger.warning("ChromaDB 未就绪，无法执行向量化。")
        return

    logger.info("开始同步 MaritimeNews 到本地向量库...")
    
    session_factory = get_async_session()
    async with session_factory() as db:
        stmt = select(MaritimeNews).order_by(MaritimeNews.created_at.desc()).limit(100)
        result = await db.execute(stmt)
        news_list = result.scalars().all()
        
    if not news_list:
        return

    # 获取当前 Chroma 中已有的 ID，用于增量去重
    existing_data = news_collection.get(include=["metadatas"])
    existing_ids = set(existing_data["ids"]) if existing_data and "ids" in existing_data else set()

    docs_to_embed = []
    metadatas = []
    ids = []

    for news in news_list:
        doc_id = f"news_{news.id}"
        if doc_id in existing_ids:
            continue
            
        # 组装待向量化的文本：标题 + 来源 + 内容摘要
        # 限制长度以防止超出 embedding 模型 token 上限
        text_to_embed = f"Title: {news.title}\nSource: {news.source}\nContent: {news.content}"
        text_to_embed = text_to_embed[:3000]  # 简单截断策略
        
        docs_to_embed.append(text_to_embed)
        ids.append(doc_id)
        metadatas.append({
            "source": news.source or "unknown",
            "published_at": str(news.published_at),
            "db_id": news.id,
        })
        
    if not docs_to_embed:
        logger.info("向量库已是最新，没有需要同步的新闻。")
        return

    # 分批调用 Embedding (防止一次性传入过多)
    BATCH_SIZE = 10
    total_vectors = []
    
    for i in range(0, len(docs_to_embed), BATCH_SIZE):
        batch_texts = docs_to_embed[i:i+BATCH_SIZE]
        batch_vectors = await embed_texts(batch_texts)
        if not batch_vectors:
            logger.error("在同步过程中遇到 Embedding 失败，终止本次同步任务。")
            return
        total_vectors.extend(batch_vectors)

    # 存入 ChromaDB
    try:
        news_collection.add(
            embeddings=total_vectors,
            documents=docs_to_embed,
            metadatas=metadatas,
            ids=ids
        )
        logger.info(f"成功将 {len(ids)} 条新闻向量化并存入 ChromaDB。")
    except Exception as e:
        logger.error(f"写入 ChromaDB 失败: {e}")


async def retrieve_context(query: str, top_k: int = 3) -> list[dict]:
    """
    根据用户提问，在本地向量库中搜索最相关的上下文。
    """
    if not news_collection:
        return []

    # 1. 向量化用户 Query
    query_vectors = await embed_texts([query])
    if not query_vectors:
        return []

    # 2. 相似度检索
    try:
        results = news_collection.query(
            query_embeddings=query_vectors,
            n_results=top_k,
            include=["documents", "metadatas", "distances"]
        )
        
        if not results or not results["ids"] or not results["ids"][0]:
            return []
            
        # 组装返回结果
        retrieved_contexts = []
        for idx in range(len(results["ids"][0])):
            retrieved_contexts.append({
                "id": results["ids"][0][idx],
                "document": results["documents"][0][idx],
                "metadata": results["metadatas"][0][idx],
                "distance": results["distances"][0][idx],
            })
            
        return retrieved_contexts
        
    except Exception as e:
        logger.error(f"ChromaDB 检索异常: {e}")
        return []
