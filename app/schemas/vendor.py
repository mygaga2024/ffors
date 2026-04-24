"""
FFORS 供应商数据校验层 (Pydantic Schemas)
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class VendorBase(BaseModel):
    """供应商基础字段。"""
    name: str = Field(..., min_length=1, max_length=100, description="供应商名称")
    vendor_type: str = Field(
        default="forwarder",
        description="类型: carrier(船司) / forwarder(货代)",
    )
    contact_info: Optional[dict] = Field(default=None, description="联系信息")
    reliability_score: Optional[float] = Field(
        default=50.0, ge=0, le=100, description="可靠性评分 (0-100)"
    )
    is_active: bool = Field(default=True, description="是否启用")


class VendorCreate(VendorBase):
    """创建供应商请求体。"""
    pass


class VendorUpdate(BaseModel):
    """更新供应商请求体（所有字段可选）。"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    vendor_type: Optional[str] = None
    contact_info: Optional[dict] = None
    reliability_score: Optional[float] = Field(None, ge=0, le=100)
    is_active: Optional[bool] = None


class VendorResponse(VendorBase):
    """供应商响应数据。"""
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}
