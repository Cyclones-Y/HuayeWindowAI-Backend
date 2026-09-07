"""公开方案、筛选条件与样例场景的稳定前端契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator


class RoomType(StrEnum):
    LIVING_ROOM = "LIVING_ROOM"
    BEDROOM = "BEDROOM"
    BALCONY = "BALCONY"


class NeedType(StrEnum):
    QUIET = "QUIET"
    INSULATION = "INSULATION"
    SAFETY = "SAFETY"
    VENTILATION = "VENTILATION"


class CatalogModel(BaseModel):
    """内部使用 snake_case，HTTP 响应沿用小程序的 camelCase 字段名。"""

    model_config = {"populate_by_name": True}


class FrameColor(CatalogModel):
    code: str
    label: str
    color_hex: str = Field(alias="colorHex", pattern=r"^#[0-9A-Fa-f]{6}$")
    available: bool


class GlassOption(CatalogModel):
    code: str
    label: str
    available: bool


class SolutionSummary(CatalogModel):
    id: str
    name: str
    subtitle: str
    thumbnail_url: str = Field(alias="thumbnailUrl")
    room: RoomType
    needs: list[NeedType]
    features: list[str]
    pricing_note: str = Field(alias="pricingNote")
    available: bool


class SolutionPage(CatalogModel):
    items: list[SolutionSummary]
    page: int
    size: int
    total: int
    has_next: bool = Field(alias="hasNext")


class SolutionDetail(CatalogModel):
    id: str
    name: str
    subtitle: str
    description: str
    cover_url: str = Field(alias="coverUrl")
    gallery_urls: list[str] = Field(alias="galleryUrls")
    room: RoomType
    needs: list[NeedType]
    features: list[str]
    pricing_note: str = Field(alias="pricingNote")
    frame_colors: list[FrameColor] = Field(alias="frameColors")
    glass_options: list[GlassOption] = Field(alias="glassOptions")
    default_frame_color_code: str = Field(alias="defaultFrameColorCode")
    default_glass_code: str = Field(alias="defaultGlassCode")
    available: bool


class NormalizedSelection(CatalogModel):
    """选区基于原图归一化，宽高必须为正且右下角不能超出图片。"""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("样例默认选区不得超出图片边界")
        return self


class SampleSceneData(CatalogModel):
    id: str
    name: str
    scene_type: RoomType = Field(alias="sceneType")
    thumbnail_url: str = Field(alias="thumbnailUrl")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    default_selection: NormalizedSelection = Field(alias="defaultSelection")


class SampleSceneList(CatalogModel):
    items: list[SampleSceneData]
