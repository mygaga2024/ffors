"""
FFORS 港口数据校验层 (Pydantic Schemas)
"""

from typing import Optional

from pydantic import BaseModel, Field


class PortResponse(BaseModel):
    """港口响应数据。"""
    code: str = Field(..., description="UN/LOCODE 港口代码")
    name_en: str = Field(..., description="英文名")
    name_cn: Optional[str] = Field(None, description="中文名")
    country: Optional[str] = Field(None, description="国家")
    region: Optional[str] = Field(None, description="区域")
    aliases: Optional[list] = Field(default=None, description="别名列表")

    model_config = {"from_attributes": True}
