"""
FFORS 核心报价表 (OceanRate)
存储海运报价明细，包含航线、价格、时效及量化预留字段。
修改本文件需遵循 DEVELOPMENT_PROTOCOL.md §1 锁定既定逻辑原则。
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from app.models.base import Base


class OceanRate(Base):
    """海运报价核心表。"""

    __tablename__ = "ocean_rates"

    # --- 基础标识 ---
    id = Column(Integer, primary_key=True, autoincrement=True)

    # --- 航线信息 ---
    pol_code = Column(
        String(10),
        ForeignKey("ports.code"),
        nullable=False,
        comment="起运港代码 (FK→Port)",
    )
    pod_code = Column(
        String(10),
        ForeignKey("ports.code"),
        nullable=False,
        comment="目的港代码 (FK→Port)",
    )

    # --- 商务信息 ---
    carrier = Column(String(50), nullable=False, comment="船公司")
    vendor_id = Column(
        Integer,
        ForeignKey("vendors.id"),
        nullable=True,
        comment="报价供应商 (FK→Vendor)",
    )

    # --- 价格信息 ---
    price_20gp = Column(Numeric(10, 2), nullable=True, comment="20GP 价格 (USD)")
    price_40gp = Column(Numeric(10, 2), nullable=True, comment="40GP 价格 (USD)")
    price_40hq = Column(Numeric(10, 2), nullable=True, comment="40HQ 价格 (USD)")
    currency = Column(String(5), nullable=False, default="USD", comment="币种")

    # --- 时效信息 ---
    etd = Column(Date, nullable=True, comment="预计开航日")
    tt_days = Column(Integer, nullable=True, comment="航程天数")
    valid_from = Column(Date, nullable=True, comment="报价生效日")
    valid_to = Column(Date, nullable=True, comment="报价失效日")

    # --- 量化分析字段 ---
    wow_20gp = Column(Float, nullable=True, comment="20GP 环比变化率 (%)")
    mom_20gp = Column(Float, nullable=True, comment="20GP 月同比变化率 (%)")
    wow_40gp = Column(Float, nullable=True, comment="40GP 环比变化率 (%)")
    mom_40gp = Column(Float, nullable=True, comment="40GP 月同比变化率 (%)")

    # --- 智能预留字段 (Phase 2 填充) ---
    risk_score = Column(Float, nullable=True, comment="风险评分 (0-100)")
    ai_summary = Column(Text, nullable=True, comment="AI 分析摘要")

    # --- 附加信息 ---
    remarks = Column(Text, nullable=True, comment="备注")
    source_file = Column(String(255), nullable=True, comment="来源文件名")

    # --- 审计字段 ---
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="入库时间",
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=lambda: datetime.now(timezone.utc),
        comment="最后更新时间",
    )

    # --- 复合索引 ---
    __table_args__ = (
        Index("ix_rate_route", "pol_code", "pod_code", "carrier", "etd"),
        Index("ix_rate_vendor", "vendor_id"),
        Index("ix_rate_valid_to", "valid_to"),
    )

    def __repr__(self):
        return (
            f"<OceanRate(id={self.id}, "
            f"{self.pol_code}→{self.pod_code}, "
            f"carrier='{self.carrier}', "
            f"20GP={self.price_20gp})>"
        )
