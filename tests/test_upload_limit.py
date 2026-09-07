"""上传入口在 multipart 解析前拒绝超量数据，避免仅在业务函数中限长的漏洞。"""

import json

import pytest
from starlette.types import Message, Receive, Scope, Send

from huaye_window_ai.core.upload_limit import PhotoUploadLimitMiddleware


@pytest.mark.parametrize("declared_length", [None, b"1", b"invalid"])
async def test_streamed_upload_limit_cannot_be_bypassed_by_content_length(declared_length: bytes | None) -> None:
    """缺失、错误和伪造的长度头都要按实际字节拒绝，解析器不能收到超量内容。"""

    reached_application = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reached_application
        reached_application = True

    middleware = PhotoUploadLimitMiddleware(app, upload_path="/api/v1/photos", max_file_bytes=1024)
    messages = iter(
        [
            {"type": "http.request", "body": b"x" * (64 * 1024), "more_body": True},
            {"type": "http.request", "body": b"x" * 1025, "more_body": False},
        ]
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return next(messages)

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/photos",
        "headers": [] if declared_length is None else [(b"content-length", declared_length)],
        "state": {"request_id": "req_test_upload"},
    }
    await middleware(scope, receive, send)
    assert not reached_application
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["requestId"] == "req_test_upload"
    assert json.loads(sent[1]["body"])["code"] == "PHOTO_TOO_LARGE"


async def test_bounded_upload_is_replayed_without_changing_bytes() -> None:
    delivered = bytearray()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            delivered.extend(message["body"])
            if not message.get("more_body", False):
                return

    chunks = iter([b"multipart-boundary", b"", b"-file-content"])

    async def receive() -> Message:
        chunk = next(chunks)
        return {"type": "http.request", "body": chunk, "more_body": chunk != b"-file-content"}

    async def send(message: Message) -> None:
        raise AssertionError("正常请求不应被上传限制中间件直接响应")

    scope: Scope = {"type": "http", "method": "POST", "path": "/v1/photos", "headers": []}
    await PhotoUploadLimitMiddleware(app, upload_path="/v1/photos", max_file_bytes=1024)(scope, receive, send)
    assert delivered == b"multipart-boundary-file-content"
