"""在 multipart 解析之前限制照片请求总大小，覆盖分块上传与不可信长度头。"""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class PhotoUploadLimitMiddleware:
    """仅缓冲照片上传请求，限制内存与临时文件占用；其余请求保持原有流式行为。

    FastAPI 会先解析 multipart 再进入上传函数，因此仅在 UploadFile.read 时检查大小
    无法阻止超大请求先落临时磁盘。这里最多保留一个受限请求，另给表单边界 64 KiB。
    """

    def __init__(self, app: ASGIApp, *, upload_path: str, max_file_bytes: int) -> None:
        self.app = app
        self.upload_path = upload_path
        self.max_body_bytes = max_file_bytes + 64 * 1024

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") != self.upload_path:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        try:
            declared_size = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared_size = 0  # 仍按实际接收字节计数，不依赖客户端报告值。
        if declared_size > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            # 合并分块，避免大量零字节块造成列表项无限增长。
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay() -> Message:
            """一次交给 multipart 解析器，耗尽后继续转发客户端断开事件。"""

            nonlocal replayed
            if not replayed:
                replayed = True
                data = bytes(body)
                body.clear()
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        """中间件直接响应时仍遵守业务信封，并向外层访问日志交付安全错误摘要。"""

        state: dict[str, Any] = scope.setdefault("state", {})
        state["access_error_code"] = "PHOTO_TOO_LARGE"
        state["access_error_message"] = "上传请求超过照片大小限制"
        response = JSONResponse(
            status_code=413,
            content={
                "code": "PHOTO_TOO_LARGE",
                "message": state["access_error_message"],
                "data": None,
                "requestId": state.get("request_id", "req_unknown"),
            },
        )
        await response(scope, receive, send)
