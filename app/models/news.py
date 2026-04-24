"""
FFORS 航运新闻流表 (MaritimeNews)
存储从 RSS 抓取的外部资讯，作为后续 RAG 模块的上下文语料。
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text
from app.models.base import Base


class MaritimeNews(Base):
    """航运资讯与新闻流表"""

    __tablename__ = "maritime_news"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(500), nullable=False, comment="新闻标题")
    link = Column(String(1000), nullable=False, unique=True, index=True, comment="原文链接 (用于去重)")
    published_at = Column(DateTime(timezone=True), nullable=True, index=True, comment="发布时间")
    source = Column(String(100), nullable=True, comment="信息来源 (如 Splash247, 搜航网)")
    summary = Column(Text, nullable=True, comment="摘要")
    content = Column(Text, nullable=True, comment="清洗后的纯文本正文")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="系统抓取时间",
    )

    def __repr__(self):
        return f"<MaritimeNews(id={self.id}, title='{self.title[:20]}...', source='{self.source}')>"
