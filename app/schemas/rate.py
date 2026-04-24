"""
FFORS 报价数据校验层 (Pydantic Schemas)
定义 API 的请求/响应数据结构，严格校验字段合法性。
契约锁定：字段名称与类型一经确认，未经允许不得修改 (DEVELOPMENT_PROTOCOL.md §4)
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────
# 基础校验规则
# ─────────────────────────────────────────────

class RateBase(BaseModel):
    """报价单基础字段（新建和更新共用）。"""

    pol_code: str = Field(..., min_length=2, max_length=10, description="起运港代码 (如 CNSHA)")
    pod_code: str = Field(..., min_length=2, max_length=10, description="目的港代码 (如 NLRTM)")
    carrier: str = Field(..., min_length=1, max_length=50, description="船公司")
    vendor_id: Optional[int] = Field(None, description="报价供应商 ID")

    price_20gp: Optional[Decimal] = Field(None, ge=0, decimal_places=2, description="20GP 价格 (USD)")
    price_40gp: Optional[Decimal] = Field(None, ge=0, decimal_places=2, description="40GP 价格 (USD)")
    price_40hq: Optional[Decimal] = Field(None, ge=0, decimal_places=2, description="40HQ 价格 (USD)")
    currency: str = Field(default="USD", max_length=5, description="币种")

    etd: Optional[date] = Field(None, description="预计开航日")
    tt_days: Optional[int] = Field(None, ge=0, le=365, description="航程天数")
    valid_from: Optional[date] = Field(None, description="报价生效日")
    valid_to: Optional[date] = Field(None, description="报价失效日")

    remarks: Optional[str] = Field(None, max_length=1000, description="备注")

    @field_validator("pol_code", "pod_code", mode="before")
    @classmethod
    def uppercase_port_codes(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("currency", mode="before")
    @classmethod
    def uppercase_currency(cls, v: str) -> str:
        return v.strip().upper()


# ─────────────────────────────────────────────
# 创建报价 (POST)
# ─────────────────────────────────────────────

class RateCreate(RateBase):
    """创建单条报价的请求体。"""
    pass


# ─────────────────────────────────────────────
# 报价响应 (Response)
# ─────────────────────────────────────────────

class RateResponse(RateBase):
    """返回给客户端的报价数据结构。"""

    id: int
    wow_change: Optional[float] = Field(None, description="环比变化率 (%)")
    mom_change: Optional[float] = Field(None, description="月同比变化率 (%)")
    source_file: Optional[str] = Field(None, description="来源文件名")
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ─────────────────────────────────────────────
# 批量导入结果 (Import Result)
# ─────────────────────────────────────────────

class ImportError(BaseModel):
    """单条导入失败的错误详情。"""
    row: int = Field(..., description="Excel 行号（从 2 开始，1 为表头）")
    reason: str = Field(..., description="失败原因")
    raw_data: Optional[dict] = Field(None, description="原始行数据（已脱敏）")


class RateBatchImportResult(BaseModel):
    """Excel 批量导入的汇总结果。"""
    total: int = Field(..., description="总行数")
    success: int = Field(..., description="成功导入数")
    failed: int = Field(..., description="失败数")
    errors: list[ImportError] = Field(default_factory=list, description="失败详情列表")
    source_file: str = Field(..., description="上传文件名")
