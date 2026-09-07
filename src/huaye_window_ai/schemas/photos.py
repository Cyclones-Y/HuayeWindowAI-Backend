"""照片上传的公开响应，只提供有限时效的预览地址，不暴露存储对象 key。"""

from datetime import datetime

from pydantic import BaseModel, Field


class UploadedPhotoData(BaseModel):
    """尺寸和字节数均属于纠正方向、清除元数据后的 JPEG，供前端框选使用。"""

    photo_id: str = Field(alias="photoId")
    width: int
    height: int
    mime_type: str = Field(alias="mimeType")
    size_bytes: int = Field(alias="sizeBytes")
    preview_url: str = Field(alias="previewUrl")
    preview_url_expires_at: datetime = Field(alias="previewUrlExpiresAt")
    created_at: datetime = Field(alias="createdAt")

    model_config = {"populate_by_name": True}
