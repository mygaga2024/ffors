"""
FFORS 供应商表 (Vendor)
存储船司、货代等供应商的基本信息与评分。
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String
from app.models.base import Base


class Vendor(Base):
    """供应商信息表。"""

    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True, comment="供应商名称")
    vendor_type = Column(
        String(20),
        nullable=False,
        default="forwarder",
        comment="类型: carrier(船司) / forwarder(货代)",
    )
    contact_info = Column(JSON, nullable=True, default=dict, comment="联系信息")
    reliability_score = Column(Float, nullable=True, default=50.0, comment="可靠性评分 (0-100)")
    is_active = Column(Boolean, nullable=False, default=True, comment="是否启用")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="创建时间",
    )

    def __repr__(self):
        return f"<Vendor(name='{self.name}', type='{self.vendor_type}')>"
