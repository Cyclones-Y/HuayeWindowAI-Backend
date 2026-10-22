"""受保护的生成任务、结果历史与清理接口，全部沿用统一信封。"""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from huaye_window_ai.api.dependencies import get_current_user_id
from huaye_window_ai.core.responses import ApiResponse
from huaye_window_ai.db.render_models import RenderResult, RenderTask
from huaye_window_ai.db.session import get_db_session
from huaye_window_ai.schemas.render import CreateRenderTask
from huaye_window_ai.services.render_queue import expire_render_tasks
from huaye_window_ai.services.renders import RenderService, owned, task_data

router = APIRouter(tags=["真实生成"])


def get_render_service(request: Request) -> RenderService:
    """应用资源可在测试中替换，路由不构造或调用供应商客户端。"""
    return cast(RenderService, request.app.state.render_service)


async def refresh_owned_task(request: Request, session: AsyncSession, task_id: str, user_id: str) -> RenderTask:
    """先验证归属，再结束过期等待；离线 worker 不会阻止查询、重试和清理。"""
    task = await owned(session, RenderTask, task_id, user_id)
    await expire_render_tasks(session, request.app.state.settings, task_id=task_id, user_id=user_id)
    await session.commit()
    # 条件更新故意不依赖 ORM 缓存，刷新后返回竞争结束后的数据库真实状态。
    await session.refresh(task)
    return task


@router.post("/render-tasks", status_code=202, response_model=ApiResponse[dict[str, Any]])
async def create_task(
    request: Request,
    body: CreateRenderTask,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
    service: RenderService = Depends(get_render_service),
) -> ApiResponse[dict[str, Any]]:
    """任务提交即返回，不同步等待 AI 输出。"""
    return ApiResponse(
        data=task_data(await service.create(body, user_id, key, session)), request_id=request.state.request_id
    )


@router.get("/render-tasks/{task_id}", response_model=ApiResponse[dict[str, Any]])
async def get_task(
    task_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> ApiResponse[dict[str, Any]]:
    """轮询当前用户任务并兜底超时回收，不触发生成或重试。"""
    return ApiResponse(
        data=task_data(await refresh_owned_task(request, session, task_id, user_id)),
        request_id=request.state.request_id,
    )


@router.post("/render-tasks/{task_id}/retry", status_code=202, response_model=ApiResponse[dict[str, Any]])
async def retry_task(
    task_id: str,
    request: Request,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
    service: RenderService = Depends(get_render_service),
) -> ApiResponse[dict[str, Any]]:
    """显式创建关联重试任务，不修改旧任务失败终态。"""
    await refresh_owned_task(request, session, task_id, user_id)
    return ApiResponse(
        data=task_data(await service.retry(task_id, user_id, key, session)), request_id=request.state.request_id
    )


@router.get("/render-results", response_model=ApiResponse[dict[str, Any]])
async def list_results(
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
    service: RenderService = Depends(get_render_service),
) -> ApiResponse[dict[str, Any]]:
    """服务端历史支持换设备恢复，样例与住宅结果保留明确类型。"""
    return ApiResponse(
        data=await service.list_results(user_id, page, size, session), request_id=request.state.request_id
    )


@router.get("/render-results/{result_id}", response_model=ApiResponse[dict[str, Any]])
async def get_result(
    result_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
    service: RenderService = Depends(get_render_service),
) -> ApiResponse[dict[str, Any]]:
    """刷新签名图片地址，不返回供应商临时地址。"""
    return ApiResponse(
        data=await service.result(await owned(session, RenderResult, result_id, user_id), session),
        request_id=request.state.request_id,
    )


@router.delete("/render-results/{result_id}", response_model=ApiResponse[None])
async def delete_result(
    result_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
    service: RenderService = Depends(get_render_service),
) -> ApiResponse[None]:
    """删除失败不会被伪装为成功，墓碑允许重试底层对象清理。"""
    await service.delete_result(result_id, user_id, session)
    return ApiResponse(data=None, request_id=request.state.request_id)


@router.delete("/render-tasks/{task_id}", response_model=ApiResponse[None])
async def delete_task_input(
    task_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
    service: RenderService = Depends(get_render_service),
) -> ApiResponse[None]:
    """设备清理使用：拒绝进行中任务，幂等回收失败任务的私有快照。"""
    await refresh_owned_task(request, session, task_id, user_id)
    await service.delete_task_input(task_id, user_id, session)
    return ApiResponse(data=None, request_id=request.state.request_id)
