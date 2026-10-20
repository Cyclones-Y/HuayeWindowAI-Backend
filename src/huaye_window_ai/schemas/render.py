"""真实生成 HTTP 契约；阶段模式不向用户返回推算的百分比。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from huaye_window_ai.schemas.catalog import CatalogModel, NormalizedSelection


class CreateRenderTask(CatalogModel):
    """引用 ID 与选区由前端提供，具体产品配置由后端校验并快照。"""

    source_type: Literal["UPLOADED_PHOTO", "SAMPLE_SCENE"] = Field(alias="sourceType")
    photo_id: str | None = Field(default=None, alias="photoId")
    sample_scene_id: str | None = Field(default=None, alias="sampleSceneId")
    selection: NormalizedSelection
    solution_id: str = Field(alias="solutionId")
    frame_color_code: str = Field(alias="frameColorCode")
    glass_code: str = Field(alias="glassCode")
    client_request_id: UUID = Field(alias="clientRequestId")

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        """拒绝模糊来源，不能同时用样例 ID 和住宅照片 ID 创建任务。"""
        if self.source_type == "UPLOADED_PHOTO":
            valid = bool(self.photo_id) and self.sample_scene_id is None
        else:
            valid = bool(self.sample_scene_id) and self.photo_id is None
        if not valid:
            raise ValueError("照片和样例必须按 sourceType 二选一")
        return self
