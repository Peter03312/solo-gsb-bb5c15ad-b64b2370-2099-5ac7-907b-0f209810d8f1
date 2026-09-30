"""HTTP 请求模型（Pydantic v2）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class MaterialCreate(BaseModel):
    lot_no: str = Field(min_length=1, description="原料批号（全局唯一）")
    material_name: str = Field(min_length=1, description="原料名称")
    allergens: list[str] = Field(default_factory=list, description="所含过敏原（代码或中文名）")


class MaterialCorrection(BaseModel):
    """原料批号主数据更正：只传需要修改的字段。"""

    material_name: str | None = Field(default=None, min_length=1)
    allergens: list[str] | None = None


class BatchCreate(BaseModel):
    batch_no: str = Field(min_length=1, description="批次号（同一产线内唯一）")
    line_code: str | None = Field(default=None, description="产线标识，默认 L1")
    material_lots: list[str] = Field(min_length=1, description="本批所用原料批号列表")
    declared_allergens: list[str] = Field(default_factory=list, description="成品声明过敏原")
    cleaning: list[str] = Field(
        default_factory=list,
        description="批次开始前清洁核验确认已清除的过敏原",
    )


class BatchCorrection(BaseModel):
    """批次更正：只传需要修改的字段；原料批号/声明/清洁均可更正。"""

    material_lots: list[str] | None = Field(default=None, min_length=1)
    declared_allergens: list[str] | None = None
    cleaning: list[str] | None = None
