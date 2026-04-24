"""
FFORS 港口映射表 (Port)
存储全球海运港口的标准代码、名称和别名映射。
"""

from sqlalchemy import Column, Integer, String, JSON
from app.models.base import Base


class Port(Base):
    """港口标准代码映射表。"""

    __tablename__ = "ports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), unique=True, nullable=False, index=True, comment="UN/LOCODE 港口代码")
    name_en = Column(String(100), nullable=False, comment="英文名称")
    name_cn = Column(String(100), nullable=True, comment="中文名称")
    country = Column(String(50), nullable=False, comment="国家/地区")
    region = Column(String(50), nullable=True, comment="所属区域")
    aliases = Column(JSON, nullable=True, default=list, comment="别名列表")

    def __repr__(self):
        return f"<Port(code='{self.code}', name_en='{self.name_en}')>"
