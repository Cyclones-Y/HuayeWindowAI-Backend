"""与小程序接口文档一致的统一响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ApiResponse[T](BaseModel):
    code: str = "OK"
    message: str = "success"
    data: T
    request_id: str = Field(alias="requestId")

    model_config = {"populate_by_name": True}


class ErrorResponse(ApiResponse[dict[str, Any] | None]):
    code: str
    message: str
    data: dict[str, Any] | None = None
