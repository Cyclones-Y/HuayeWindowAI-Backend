"""Seedream 同步 HTTP 适配；业务 worker 提供异步任务，供应商请求不自动重发。"""

import base64
from typing import Protocol

import httpx

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.errors import AppError


class ImageModelProvider(Protocol):
    """供应商替换边界；仅返回图像字节，不把供应商地址或密钥暴露给业务页面。"""

    async def generate(self, images: list[bytes], prompt: str, size: tuple[int, int], *, model: str) -> bytes: ...


class SeedreamProvider:
    """使用独立长超时客户端，不能复用微信登录的五秒超时。"""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings, self._http = settings, http

    async def generate(self, images: list[bytes], prompt: str, size: tuple[int, int], *, model: str) -> bytes:
        """Base64 输入适用于局域网开发；禁止自动重试，避免未知结果重复计费。"""
        response = await self._http.post(
            f"{self._settings.ark_base_url}/images/generations",
            headers={"Authorization": f"Bearer {self._settings.ark_api_key.get_secret_value()}"},
            json={
                "model": model,
                "prompt": prompt,
                "image": ["data:image/png;base64," + base64.b64encode(data).decode() for data in images],
                "size": f"{size[0]}x{size[1]}",
                "response_format": "b64_json",
                "output_format": "png",
                "stream": False,
                "watermark": False,
                # Pro 本身只生成单张，不支持 sequential_image_generation 参数。
                "optimize_prompt_options": {"mode": "standard"},
            },
        )
        if response.status_code == 429:
            raise AppError("RENDER_PROVIDER_BUSY", "模型服务繁忙，请稍后重试", status_code=503)
        if response.status_code in {401, 403}:
            raise AppError("RENDER_PROVIDER_CONFIGURATION", "生成服务配置异常，请联系管理员", status_code=503)
        if response.status_code >= 400:
            raise AppError("RENDER_PROVIDER_REJECTED", "生成服务未完成本次请求，请调整照片后重试", status_code=502)
        try:
            encoded = response.json()["data"][0]["b64_json"]
            if len(encoded) > 28_000_000:
                raise ValueError("size limit")
            return base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, ValueError, TypeError):
            raise AppError("RENDER_PROVIDER_INVALID_RESPONSE", "生成服务返回异常，请重试", status_code=502) from None
