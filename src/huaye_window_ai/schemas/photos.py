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


class AccountPhotoItem(BaseModel):
    """账户清理用的最小照片信息；不签发预览链接，也不公开存储对象路径。"""

    photo_id: str = Field(alias="photoId")
    created_at: datetime = Field(alias="createdAt")
    deleted: bool

    model_config = {"populate_by_name": True}


class AccountPhotoPage(BaseModel):
    """以不可变照片 ID 游标分页，删除当前页不会让后续清理跳过记录。"""

    items: list[AccountPhotoItem]
    next_cursor: str | None = Field(alias="nextCursor")

    model_config = {"populate_by_name": True}
