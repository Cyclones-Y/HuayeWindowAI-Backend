# Seedream 5.0 Pro 生成任务

生成由 MySQL 持久队列与独立 worker 执行。API 请求只校验并保存输入快照，返回 202；离开小程序或重启 API 不取消任务。关闭生成或缺少密钥时返回 `503 / RENDER_NOT_ENABLED`，没有演示图回退。

## 配置与启动

后端配置写入选中的 `.env.development` 或 `.env.production`（不要将密钥放入小程序或提交到 Git）：
从可提交模板 `.env.example` 复制，开发文件保留 `APP_ENV=development`，生产文件改为 `APP_ENV=production`；
实际两个文件均已被 Git 忽略。启动命令用 `--env` 选择环境，优先于进程 `APP_ENV`；其余配置仍允许进程变量覆盖。
不读取基础 `.env` 或叠加另一环境文件，生产配置缺失不得回退到开发。

```dotenv
RENDER_ENABLED=true
ARK_API_KEY=填写具备模型权限的密钥
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
SEEDREAM_MODEL=doubao-seedream-5-0-pro-260628
RENDER_WORKER_CONCURRENCY=2
RENDER_PROVIDER_TIMEOUT_SECONDS=300
RENDER_TASK_TIMEOUT_SECONDS=420
RENDER_QUEUE_MAX_WAIT_SECONDS=900
RENDER_MAINTENANCE_INTERVAL_SECONDS=5
RENDER_WORKER_HEARTBEAT_INTERVAL_SECONDS=5
RENDER_WORKER_HEARTBEAT_TTL_SECONDS=20
```

本次可靠性修复新增迁移 `20261020_0004`（`photo_write_intents` 表），需先在临时数据库验证、备份并审核后再升级业务库。本次开发验证不升级当前业务数据库。API 与 worker 应一起升级到包含该迁移的版本，旧写入流程没有持久登记。

```powershell
# 开发；生产改为 production，并先核对 .env.production
uv run python -m huaye_window_ai --env development migrate
uv run python -m huaye_window_ai --env development worker
```

PowerShell 与 Linux 使用相同命令，生产直接把 `--env development` 改为 `--env production`，无需提前设置环境变量。
API 启动命令为 `uv run python -m huaye_window_ai --env development`，API 启动不会自动执行迁移。
API 与 worker 必须使用同一环境、数据库及私有存储配置。本地存储共享 `data/photos`，S3 模式共享 bucket 和访问权限。
Compose 的 worker 使用 `render` profile，配置密钥并确认真实调用费用后，按环境执行：

```powershell
# 开发
docker compose --env-file .env.development --profile render up -d --build

# 生产
docker compose --env-file .env.production --profile render up -d --build
```

Linux 使用相同的 Compose 命令。文件内的 `APP_ENV` 必须和所选文件名一致；
若终端已额外设置 `APP_ENV`，也需与该文件一致。
`--env-file` 负责 Compose 插值，API/worker 的 `env_file` 才将该文件注入容器。
API 健康后 worker 才启动，API 不提供请求内后台生图。

`/health/live` 表示 Web 进程存活，供 Compose 的 worker 启动依赖使用；`/health/ready` 检查 MySQL/Redis，在真实生成已启用且密钥非空时还检查 `renderWorker` 心跳。worker 每 5 秒独立报活，最后一个进程离线后最多 20 秒 ready 返回 503。不要将 worker 启动依赖改为 ready，否则首次启动会互相等待。心跳表示进程在线，不代表供应商模型一定可用。

## 接口

路径相对于 `API_PREFIX`；均需 `Authorization: Bearer ...`，响应沿用 `{code,message,data,requestId}`。

| 方法与路径 | 行为 |
|---|---|
| POST `/render-tasks` | 保存任务，HTTP 202，必填 `Idempotency-Key` |
| GET `/render-tasks/{taskId}` | 获取真实状态和阶段，兜底结束过期等待，不触发模型调用 |
| POST `/render-tasks/{taskId}/retry` | 失败且可重试时创建关联新任务，HTTP 202，必填 `Idempotency-Key` |
| GET `/render-results?page=1&size=20` | 当前账户历史，返回 items/page/size/total/hasNext |
| GET `/render-results/{resultId}` | 配置快照与重新签发的私有图片地址 |
| DELETE `/render-results/{resultId}` | 幂等禁用记录并清理其独立原图和效果图 |
| DELETE `/render-tasks/{taskId}` | 设备清理补充接口：只清理终态任务输入，成功任务转为结果清理；保留幂等记录 |

创建示例：

```json
{
  "sourceType": "SAMPLE_SCENE",
  "sampleSceneId": "sample_living_room_01",
  "selection": {"x": 0.2, "y": 0.2, "width": 0.6, "height": 0.6},
  "solutionId": "solution-living-panorama",
  "frameColorCode": "SPACE_GRAY",
  "glassCode": "DOUBLE_HOLLOW",
  "clientRequestId": "8cf1b061-4888-4d58-a8f1-850372cd7e17"
}
```

住宅照片使用 `sourceType: UPLOADED_PHOTO` 与 `photoId`，不能同时提供 `sampleSceneId`。选区为纠正方向后的原图坐标，x/y/width/height 均在 0—1 范围且不越界。方案、颜色、玻璃必须可用；自由提示词不属于接口。Pydantic 输入错误沿用现有 HTTP 400 信封，业务配置错误为 422。越权 403、不存在 404、幂等冲突 409。

同一账户使用原 key 和原请求可恢复响应丢失的创建；即使 key 改变，相同 `clientRequestId` 也恢复原任务。相同标识绑定不同内容返回 409。重试 key 不变始终对应同一个新任务；用户主动新建重试可能产生新的模型费用。

主动重试复制失败任务的私有原图、方案配置和模型版本，不读取后来变更的草稿或源照片。目录下架不改变该快照；已清理输入的旧任务不能新建重试，但此前已受理的同 key 重试仍可恢复。

任务示例：

```json
{
  "taskId": "task_...", "status": "PROCESSING", "stage": "GENERATING",
  "stageLabel": "AI 正在生成", "progress": null, "pollAfterMs": 3000,
  "resultId": null, "failure": null
}
```

状态 `QUEUED → PROCESSING → SUCCEEDED / FAILED`；阶段 `QUEUED → PREPARING → GENERATING → SAVING → COMPLETED`。排队/模型生成轮询 3000ms，准备/保存 1500ms。失败保留实际阶段，failure 提供 code/message/retryable/stage。供应商没有模型内部百分比，不返回推算数值。

结果提供 id/taskId/resultType/width/height/solution/beforeImageUrl/afterImageUrl/imageUrlExpiresAt/createdAt/disclaimer。resultType 为住宅 `USER_GENERATED` 或样例 `SAMPLE_DEMO`；两者都是真实 AI 生成。solution 是生成时的配置快照，目录下架不影响回看。图片过期重新读详情；住宅原始照片删除后，已完成结果仍持有独立原图。

## 队列、图像与清理约束

- 多执行槽通过条件更新竞争，30 秒租约、10 秒心跳。旧 token 和过期租约不能写回结果；每任务最多一条结果。
- 供应商调用前持久化 GENERATING。PREPARING 中断可重新领取；调用后的超时或失联记录失败，绝不自动重发模型请求。API 和 worker 不自动重试付费调用。
- QUEUED 从最初入队起最多等待 `RENDER_QUEUE_MAX_WAIT_SECONDS`（默认 900 秒），超时标记 FAILED/RENDER_QUEUE_TIMEOUT，用户可手动重试或清理。API 查询、重试、清理前均核对过期状态；API 与 worker 每 5 秒独立维护，模型进程离线时也不会永久等待。若两种进程都关闭，则在下次启动或请求时回收。
- 请求标准模式、非流式、PNG/Base64。Pro 固定单张，不传其不支持的组图参数。使用原图、标记图、封面参考图；仅模型输入等比缩小到 2048 边长，以符合供应商图片大小限制。
- 输出约 1536² 像素并保持原比例。结果映射回原尺寸，只融合矩形区域，羽化在框内；无损 PNG 保证框外像素保持。成功状态与结果入库在同一事务提交，且图片先保存。
- 未结束任务阻止源照片删除。结果、失败快照清理先提交墓碑，再删除私有对象；失败可再清理。任务记录保留避免旧幂等请求再次触发付费调用。
- 上传原图、生成输入和结果图在写文件前独立提交 `PhotoWriteIntent`，业务事务持有登记行锁，并将 Photo 和登记移除一起提交。提交应答丢失时以新事务核对，不能因异常直接删除有效图片。登记使用不占用业务连接池的短连接，数据库需为在途写入预留额外连接，避免池满时外层事务相互等待。
- 即时补偿后保留未提交对象的登记，以便复核远端迟到 PUT；API/worker 每 60 秒处理最多 50 条超过一小时宽限期的登记，跳过正在写入的行锁，删除失败退避 5 分钟，不阻塞其他到期对象。有效 Photo 引用始终保留；数据库或存储不可用时保留登记供恢复后重试。这不会自动定位升级前没有登记的历史孤立对象，旧对象需单独核对。
- 小程序的设备清理仅处理本设备登记资源；独立的“清理账户上传照片”入口经确认后，通过 `GET /photos` 找回所有设备及索引丢失的现场上传，不清理生成输入或结果。其他设备或索引丢失的生成记录仍需运营者处理。响应未确认的创建必须先继续查看，清理操作不重新提交生成。

## 验证与真实验收

```powershell
uv run pytest -q
uv run ruff check src tests alembic
uv run mypy src
uv run python scripts/verify_mysql_render.py
```

测试使用合成图、私有临时存储和临时 SQLite 数据库；HTTP 供应商替身验证 JSON 契约。MySQL 脚本使用当前连接参数创建随机临时库，验证迁移往返、ORM 一致性及生成竞态后删除该临时库；需具备建库权限，不修改已有业务库。

真实验收需要密钥访问该模型，并在付费调用前确认：用公开样例创建一次任务，后台离开后重新查询，确认阶段来自 worker；成功后读取私有前后图、核对尺寸与框外像素，再从历史回看。失败必须具备终态和明确重试提示。当前实现默认关闭，自动化测试不执行真实模型调用。

官方接口：[火山引擎图片生成 API](https://docs.volcengine.com/docs/ark/image-generation-api?lang=zh&redirect=1)。
