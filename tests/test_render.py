"""真实数据库、私有存储与供应商替身验证生成闭环，测试不会调用付费模型。"""

import asyncio
import base64
import io
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from PIL import Image, ImageChops
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_photos import PhotoContext, make_image, photo_context  # noqa: F401

from huaye_window_ai.core.errors import AppError
from huaye_window_ai.db.base import Base
from huaye_window_ai.db.catalog_models import WindowSolution
from huaye_window_ai.db.models import User
from huaye_window_ai.db.photo_models import Photo
from huaye_window_ai.db.render_models import RenderResult, RenderTask
from huaye_window_ai.integrations.seedream import SeedreamProvider
from huaye_window_ai.repositories.catalog import seed_catalog
from huaye_window_ai.services.render_images import composite_selection, output_dimensions, png, selected_box
from huaye_window_ai.services.render_worker import RenderWorker
from huaye_window_ai.services.renders import RenderService


@pytest.fixture
async def render_context(photo_context: PhotoContext):  # noqa: F811 — 导入现有 pytest 夹具并注册。
    """使用现有上传夹具的真实身份，覆盖登录、原图与任务之间的归属关系。"""
    context = photo_context
    # 并发测试需要独立数据库连接；内存 SQLite 的单连接池会让一次 rollback 影响另一个请求。
    database_url = os.getenv("RENDER_TEST_DATABASE_URL")
    if database_url:
        # 只接受脚本新建的随机临时 MySQL 库，避免环境变量误指向业务数据。
        target = make_url(database_url)
        assert target.drivername == "mysql+asyncmy" and re.fullmatch(
            r"huaye_render_verify_[0-9a-f]{32}", target.database or ""
        )
    engine = create_async_engine(
        database_url or f"sqlite+aiosqlite:///{(context.directory / 'render.sqlite').as_posix()}", hide_parameters=True
    )
    async with engine.begin() as connection:
        if database_url:
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    context.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    context.app.state.session_factory = context.session_factory
    context.settings.render_enabled = True
    context.settings.ark_api_key = SecretStr("test-only-key")
    service = RenderService(context.settings, context.storage)
    context.app.state.render_service = service
    async with context.session_factory() as session, session.begin():
        session.add_all(
            [
                User(id="user_owner", phone_number="+8613800138000", phone_country_code="86"),
                User(id="user_other", phone_number="+8613900139000", phone_country_code="86"),
            ]
        )
        await seed_catalog(session)
    try:
        yield context
    finally:
        await engine.dispose()


def payload():
    """样例生成也必须登录，避免测试通过游客入口绕过归属验证。"""
    return {
        "sourceType": "SAMPLE_SCENE",
        "sampleSceneId": "sample_living_room_01",
        "selection": {"x": 0.2, "y": 0.2, "width": 0.6, "height": 0.6},
        "solutionId": "solution-living-panorama",
        "frameColorCode": "SPACE_GRAY",
        "glassCode": "DOUBLE_HOLLOW",
        "clientRequestId": str(uuid.uuid4()),
    }


async def create(context, body=None, key=None):
    return await context.client.post(
        "/v1/render-tasks",
        json=body or payload(),
        headers={**context.owner_headers, "Idempotency-Key": key or str(uuid.uuid4())},
    )


async def test_create_is_idempotent_and_conflicts_are_rejected(render_context):
    context = render_context
    body, key = payload(), str(uuid.uuid4())
    first = await create(context, body, key)
    assert first.status_code == 202
    assert first.json()["data"]["progress"] is None
    repeat = await create(context, body, key)
    assert repeat.json()["data"]["taskId"] == first.json()["data"]["taskId"]
    body["frameColorCode"] = "MATTE_BLACK"
    assert (await create(context, body, key)).status_code == 409
    async with context.session_factory() as session:
        assert len((await session.scalars(select(RenderTask))).all()) == 1


async def test_invalid_configuration_and_cross_user_access(render_context):
    context = render_context
    invalid = payload()
    invalid["glassCode"] = "NOT_A_PRODUCT"
    assert (await create(context, invalid)).status_code == 422
    invalid = payload()
    invalid["selection"]["width"] = 0.9
    assert (await create(context, invalid)).status_code == 400
    task = (await create(context)).json()["data"]
    assert (
        await context.client.get(f"/v1/render-tasks/{task['taskId']}", headers=context.other_headers)
    ).status_code == 403
    assert (
        await context.client.post("/v1/render-tasks", json=payload(), headers={"Idempotency-Key": str(uuid.uuid4())})
    ).status_code == 401


class FakeProvider:
    """模拟输出可解码图像；可观察阶段而不模拟百分比或执行真实付费调用。"""

    calls = 0

    async def generate(self, images, prompt, size, *, model):
        self.calls += 1
        assert len(images) == 3
        assert "太空灰" in prompt
        return make_image(image_format="PNG", size=size)


async def test_worker_persists_result_and_never_runs_same_task_twice(render_context):
    context = render_context
    task_id = (await create(context)).json()["data"]["taskId"]
    provider = FakeProvider()
    worker = RenderWorker(context.settings, context.session_factory, context.storage, provider)
    assert await worker.run_once()
    assert not await worker.run_once()
    task = (await context.client.get(f"/v1/render-tasks/{task_id}", headers=context.owner_headers)).json()["data"]
    assert task["status"] == "SUCCEEDED" and task["stage"] == "COMPLETED"
    assert task["progress"] is None and provider.calls == 1
    result = (await context.client.get(f"/v1/render-results/{task['resultId']}", headers=context.owner_headers)).json()[
        "data"
    ]
    assert result["resultType"] == "SAMPLE_DEMO"
    before = await context.client.get(result["beforeImageUrl"])
    after = await context.client.get(result["afterImageUrl"])
    assert before.status_code == after.status_code == 200
    with Image.open(io.BytesIO(before.content)) as original, Image.open(io.BytesIO(after.content)) as generated:
        assert original.size == generated.size == (810, 1215)
        assert original.getpixel((0, 0)) == generated.getpixel((0, 0))
    assert (await context.client.get("/v1/render-results", headers=context.owner_headers)).json()["data"]["total"] == 1
    assert (
        await context.client.get(f"/v1/render-results/{result['id']}", headers=context.other_headers)
    ).status_code == 403
    assert (
        await context.client.delete(f"/v1/render-results/{result['id']}", headers=context.owner_headers)
    ).status_code == 200
    assert (await context.client.get(result["afterImageUrl"])).status_code == 404
    assert (
        await context.client.delete(f"/v1/render-results/{result['id']}", headers=context.owner_headers)
    ).status_code == 200


async def test_provider_timeout_is_terminal_without_automatic_replay(render_context):
    context = render_context
    task_id = (await create(context)).json()["data"]["taskId"]

    class TimeoutProvider:
        async def generate(self, images, prompt, size, *, model):
            raise httpx.ReadTimeout("secret provider error")

    worker = RenderWorker(context.settings, context.session_factory, context.storage, TimeoutProvider())
    assert await worker.run_once()
    assert not await worker.run_once()
    response = await context.client.get(f"/v1/render-tasks/{task_id}", headers=context.owner_headers)
    task = response.json()["data"]
    assert task["status"] == "FAILED" and task["stage"] == "GENERATING"
    assert task["failure"]["code"] == "RENDER_PROVIDER_TIMEOUT"
    assert "secret" not in response.text
    retry = await context.client.post(
        f"/v1/render-tasks/{task_id}/retry", headers={**context.owner_headers, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert retry.status_code == 202 and retry.json()["data"]["taskId"] != task_id


async def test_expired_generating_lease_is_failed_not_requeued(render_context):
    context = render_context
    task_id = (await create(context)).json()["data"]["taskId"]
    async with context.session_factory() as session:
        task = await session.get(RenderTask, task_id)
        task.status, task.stage, task.worker_token = "PROCESSING", "GENERATING", "dead-worker"
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    provider = FakeProvider()
    worker = RenderWorker(context.settings, context.session_factory, context.storage, provider)
    assert not await worker.run_once()
    task = (await context.client.get(f"/v1/render-tasks/{task_id}", headers=context.owner_headers)).json()["data"]
    assert task["status"] == "FAILED" and provider.calls == 0


async def test_busy_photo_cannot_be_deleted(render_context):
    context = render_context
    photo = (await context.upload()).json()["data"]
    body = payload()
    body.update(sourceType="UPLOADED_PHOTO", photoId=photo["photoId"])
    body.pop("sampleSceneId")
    assert (await create(context, body)).status_code == 202
    response = await context.client.delete(f"/v1/photos/{photo['photoId']}", headers=context.owner_headers)
    assert response.status_code == 409 and response.json()["code"] == "PHOTO_IN_USE"


@pytest.mark.parametrize("size", [(810, 1215), (1920, 1080), (500, 500)])
def test_output_size_and_compositing_keep_every_outside_pixel(size):
    dimensions = output_dimensions(*size)
    assert 921600 <= dimensions[0] * dimensions[1] <= 4624220
    assert abs(dimensions[0] / dimensions[1] - size[0] / size[1]) < 0.03
    original = make_image(image_format="PNG", size=size)
    generated = png(Image.new("RGB", dimensions, "#ee5533"))
    selection = {"x": 0.2, "y": 0.2, "width": 0.5, "height": 0.5}
    result = composite_selection(original, generated, selection)
    with Image.open(io.BytesIO(original)) as before, Image.open(io.BytesIO(result)) as after:
        assert before.size == after.size
        assert before.getpixel((size[0] // 2, size[1] // 2)) != after.getpixel((size[0] // 2, size[1] // 2))
        # 清除框内差异后，整张差分必须为零，覆盖框外四边与羽化边界。
        difference = ImageChops.difference(before.convert("RGB"), after.convert("RGB"))
        difference.paste((0, 0, 0), selected_box(before, selection))
        assert difference.getbbox() is None


async def test_provider_http_contract(render_context):
    """直接验证付费调用的 JSON 契约，Pro 不支持组图参数，不能误传 Lite 参数。"""
    output = make_image(image_format="PNG")

    def handler(request):
        body = json.loads(request.content)
        assert request.url == "https://ark.cn-beijing.volces.com/api/v3/images/generations"
        assert request.headers["Authorization"] == "Bearer test-only-key"
        assert body["model"] == "doubao-seedream-5-0-pro-260628"
        assert body["size"] == "1536x1536" and body["stream"] is False
        assert body["response_format"] == "b64_json" and body["output_format"] == "png"
        assert "sequential_image_generation" not in body
        assert body["optimize_prompt_options"] == {"mode": "standard"}
        assert len(body["image"]) == 3 and all(value.startswith("data:image/png;base64,") for value in body["image"])
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(output).decode()}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = SeedreamProvider(render_context.settings, http)
        assert (
            await provider.generate([output] * 3, "门窗", (1536, 1536), model=render_context.settings.seedream_model)
            == output
        )


@pytest.mark.parametrize(
    "status,body,code",
    [
        (429, {}, "RENDER_PROVIDER_BUSY"),
        (403, {}, "RENDER_PROVIDER_CONFIGURATION"),
        (500, {}, "RENDER_PROVIDER_REJECTED"),
        (200, {"data": []}, "RENDER_PROVIDER_INVALID_RESPONSE"),
        (200, {"data": [{"b64_json": "bad$"}]}, "RENDER_PROVIDER_INVALID_RESPONSE"),
    ],
)
async def test_provider_errors_are_sanitized(render_context, status, body, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))) as http:
        with pytest.raises(AppError) as error:
            await SeedreamProvider(render_context.settings, http).generate(
                [], "门窗", (1536, 1536), model=render_context.settings.seedream_model
            )
        assert error.value.code == code


async def test_concurrent_worker_claim_has_one_winner(render_context):
    """两个执行槽同时领取同一排队项，也只能有一个获胜 token。"""
    context = render_context
    await create(context)
    worker = RenderWorker(context.settings, context.session_factory, context.storage, FakeProvider())
    claims = await asyncio.gather(worker.claim(), worker.claim())
    assert len([claim for claim in claims if claim is not None]) == 1


async def test_preparation_crash_can_recover_but_stale_writer_cannot_commit(render_context):
    context = render_context
    await create(context)
    worker = RenderWorker(context.settings, context.session_factory, context.storage, FakeProvider())
    claimed = await worker.claim()
    async with context.session_factory() as session:
        task = await session.get(RenderTask, claimed.id)
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    await worker.recover()
    replacement = await worker.claim()
    assert replacement.id == claimed.id and replacement.worker_token != claimed.worker_token
    from huaye_window_ai.services.render_worker import LeaseLostError

    with pytest.raises(LeaseLostError):
        await worker.change(claimed, stage="GENERATING")


async def test_saving_failure_is_terminal_without_result_or_extra_objects(render_context, monkeypatch):
    context = render_context
    task_id = (await create(context)).json()["data"]["taskId"]
    original_put = context.storage.put
    calls = 0

    async def fail_thumbnail(key, data, content_type):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AppError("PHOTO_STORAGE_UNAVAILABLE", "存储暂时不可用", status_code=503)
        await original_put(key, data, content_type)

    monkeypatch.setattr(context.storage, "put", fail_thumbnail)
    worker = RenderWorker(context.settings, context.session_factory, context.storage, FakeProvider())
    await worker.run_once()
    task = (await context.client.get(f"/v1/render-tasks/{task_id}", headers=context.owner_headers)).json()["data"]
    assert task["status"] == "FAILED" and task["stage"] == "SAVING" and task["resultId"] is None
    async with context.session_factory() as session:
        assert list(await session.scalars(select(RenderResult))) == []
    assert len(list(context.directory.rglob("*.png"))) == 2  # 仅保留原图快照及其缩略图。


async def test_result_delete_failure_can_be_retried_and_access_stays_revoked(render_context, monkeypatch):
    context = render_context
    await create(context)
    await RenderWorker(context.settings, context.session_factory, context.storage, FakeProvider()).run_once()
    result = (await context.client.get("/v1/render-results", headers=context.owner_headers)).json()["data"]["items"][0]
    original_delete = context.storage.delete

    async def fail_delete(key):
        raise AppError("PHOTO_STORAGE_UNAVAILABLE", "暂时不能清理", status_code=503)

    monkeypatch.setattr(context.storage, "delete", fail_delete)
    assert (
        await context.client.delete(f"/v1/render-results/{result['id']}", headers=context.owner_headers)
    ).status_code == 503
    assert (await context.client.get(result["afterImageUrl"])).status_code == 404
    monkeypatch.setattr(context.storage, "delete", original_delete)
    assert (
        await context.client.delete(f"/v1/render-results/{result['id']}", headers=context.owner_headers)
    ).status_code == 200
    assert not list(context.directory.rglob("*.png"))


async def test_source_deletion_and_expired_signature_do_not_destroy_result(render_context, monkeypatch):
    context = render_context
    photo = (await context.upload()).json()["data"]
    body = payload()
    body.update(sourceType="UPLOADED_PHOTO", photoId=photo["photoId"])
    body.pop("sampleSceneId")
    await create(context, body)
    await RenderWorker(context.settings, context.session_factory, context.storage, FakeProvider()).run_once()
    assert (
        await context.client.delete(f"/v1/photos/{photo['photoId']}", headers=context.owner_headers)
    ).status_code == 200
    result = (await context.client.get("/v1/render-results", headers=context.owner_headers)).json()["data"]["items"][0]
    assert (
        result["resultType"] == "USER_GENERATED"
        and (await context.client.get(result["beforeImageUrl"])).status_code == 200
    )
    async with context.session_factory() as session:
        record = await session.get(RenderResult, result["id"])
        before = await session.get(Photo, record.before_photo_id)
        expires = datetime.now(UTC) + timedelta(seconds=1)
        expired = await context.storage.signed_url(before.storage_key, before.id, expires)
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr("huaye_window_ai.integrations.photo_storage.time.time", lambda: expires.timestamp() + 60)
        assert (await context.client.get(expired)).status_code == 403
    refreshed = await context.client.get(f"/v1/render-results/{result['id']}", headers=context.owner_headers)
    assert (await context.client.get(refreshed.json()["data"]["beforeImageUrl"])).status_code == 200


async def test_failed_task_cleanup_deletes_input_snapshot_but_keeps_idempotency(render_context):
    """无成功结果的样例失败也有住宅快照，设备清理不能遗留这份私有输入。"""
    context = render_context
    body, key = payload(), str(uuid.uuid4())
    task_id = (await create(context, body, key)).json()["data"]["taskId"]
    assert (
        await context.client.delete(f"/v1/render-tasks/{task_id}", headers=context.owner_headers)
    ).status_code == 409
    async with context.session_factory() as session:
        task = await session.get(RenderTask, task_id)
        task.status = "FAILED"
        task.failure = {"code": "RENDER_FAILED", "message": "未完成", "retryable": True}
        await session.commit()
    assert (
        await context.client.delete(f"/v1/render-tasks/{task_id}", headers=context.other_headers)
    ).status_code == 403
    assert (
        await context.client.delete(f"/v1/render-tasks/{task_id}", headers=context.owner_headers)
    ).status_code == 200
    assert not list(context.directory.rglob("*.png"))
    assert (await create(context, body, key)).json()["data"]["taskId"] == task_id


async def test_two_simultaneous_creations_reuse_same_task(render_context):
    context = render_context
    body, key = payload(), str(uuid.uuid4())
    responses = await asyncio.gather(create(context, body, key), create(context, body, key))
    assert [item.status_code for item in responses] == [202, 202]
    assert responses[0].json()["data"]["taskId"] == responses[1].json()["data"]["taskId"]


async def test_retry_uses_input_and_configuration_snapshot_and_recovers_after_old_cleanup(render_context):
    """重试不依赖已删除的上传源或实时目录，旧任务清理也不破坏已接受重试的恢复。"""
    context = render_context
    source = (await context.upload()).json()["data"]
    body = payload()
    body.update(sourceType="UPLOADED_PHOTO", photoId=source["photoId"])
    body.pop("sampleSceneId")
    task_id = (await create(context, body)).json()["data"]["taskId"]
    async with context.session_factory() as session:
        old = await session.get(RenderTask, task_id)
        old.status = "FAILED"
        old.stage = "GENERATING"
        old.failure = {"code": "RENDER_FAILED", "message": "未完成", "retryable": True}
        solution = await session.get(WindowSolution, body["solutionId"])
        solution.available = False
        await session.commit()
    assert (
        await context.client.delete(f"/v1/photos/{source['photoId']}", headers=context.owner_headers)
    ).status_code == 200
    key = str(uuid.uuid4())
    retry = await context.client.post(
        f"/v1/render-tasks/{task_id}/retry", headers={**context.owner_headers, "Idempotency-Key": key}
    )
    assert retry.status_code == 202
    assert (
        await context.client.delete(f"/v1/render-tasks/{task_id}", headers=context.owner_headers)
    ).status_code == 200
    recovered = await context.client.post(
        f"/v1/render-tasks/{task_id}/retry", headers={**context.owner_headers, "Idempotency-Key": key}
    )
    assert recovered.status_code == 202 and recovered.json()["data"]["taskId"] == retry.json()["data"]["taskId"]
    assert await RenderWorker(context.settings, context.session_factory, context.storage, FakeProvider()).run_once()


async def test_mysql_busy_guard_uses_current_read_after_authentication_snapshot(render_context):
    """MySQL REPEATABLE READ 下普通 SELECT 会漏读鉴权之后刚入队的任务。"""
    if not os.getenv("RENDER_TEST_DATABASE_URL"):
        pytest.skip("此回归专门验证 MySQL 的 current read，由临时库脚本运行")
    context = render_context
    source = (await context.upload()).json()["data"]
    async with context.session_factory() as deleting_session:
        await deleting_session.get(User, "user_owner")  # 复现鉴权先建立一致性快照。
        body = payload()
        body.update(sourceType="UPLOADED_PHOTO", photoId=source["photoId"])
        body.pop("sampleSceneId")
        assert (await create(context, body)).status_code == 202
        with pytest.raises(AppError) as error:
            await context.service.delete(source["photoId"], "user_owner", deleting_session)
        assert error.value.code == "PHOTO_IN_USE"


async def test_cancellation_after_model_send_is_not_automatically_replayed(render_context):
    """进程关闭发生在供应商等待期间，恢复只能记为失败，不能第二次计费。"""
    context = render_context
    task_id = (await create(context)).json()["data"]["taskId"]
    entered = asyncio.Event()

    class InterruptedProvider:
        calls = 0

        async def generate(self, images, prompt, size, *, model):
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()

    provider = InterruptedProvider()
    worker = RenderWorker(context.settings, context.session_factory, context.storage, provider)
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    async with context.session_factory() as session:
        task = await session.get(RenderTask, task_id)
        assert task.stage == "GENERATING"
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert not await worker.run_once()
    assert provider.calls == 1


async def test_corrupt_model_image_cannot_produce_success(render_context):
    context = render_context
    task_id = (await create(context)).json()["data"]["taskId"]

    class CorruptProvider:
        async def generate(self, images, prompt, size, *, model):
            return b"not a valid image"

    await RenderWorker(context.settings, context.session_factory, context.storage, CorruptProvider()).run_once()
    task = (await context.client.get(f"/v1/render-tasks/{task_id}", headers=context.owner_headers)).json()["data"]
    assert task["status"] == "FAILED" and task["stage"] == "SAVING" and task["resultId"] is None
    assert task["failure"]["code"] == "RENDER_IMAGE_INVALID"
