# 方案、样例与照片接口

当前已实现微信登录之外的第一批 5 个业务接口。本文示例使用默认 `API_PREFIX=/v1`，实际路径以
当前实例 Swagger 为准。所有 JSON 响应沿用 `code/message/data/requestId`，ID 是字符串，时间使用 ISO 8601。

## 初始化与配置

先将 `.env.example` 复制为 `.env.development` 并填写开发配置（保留 `APP_ENV=development`），
再安装依赖，在需要启用功能的数据库执行迁移和显式种子导入：

```powershell
uv sync --all-groups
uv run python -m huaye_window_ai --env development migrate
uv run python -m huaye_window_ai --env development seed
uv run python -m huaye_window_ai --env development
```

迁移新增 `window_solutions`、`sample_scenes`、`photos` 三张表，不改已有账号数据。
导入脚本默认仅新增缺失项；`--replace` 才会覆盖已有种子方案的字段（包括上下架和排序），应按需使用。
当前三套初始目录及图片来自小程序现有素材，保留原方案 ID，方便前端接入。

生产环境使用独立的 `.env.production`（其中 `APP_ENV=production`），将上述命令的 `--env development`
改为 `--env production`；PowerShell 与 Linux 使用相同命令，无需提前设置环境变量。
环境选择以 `--env` 为准，其余配置仍允许进程变量覆盖；不读取基础 `.env` 或叠加另一文件，生产文件缺失也不回退开发配置。
两个实际环境文件均不提交；`.env.example` 仅作模板。

在选中的 `.env.development` 或 `.env.production` 中配置统一前缀与 OSS 图片域名：

```dotenv
API_PREFIX=/api/v1
PHOTO_STORAGE_BACKEND=oss
OSS_ENDPOINT=https://oss.zmintz.com
```

此配置使登录变为 `POST /api/v1/auth/wechat/login`，方案变为 `GET /api/v1/solutions`。
前端 `config/api.ts` 的 origin 填客户端能访问的后端地址，prefix 同步配置为 `/api/v1`；
OSS 域名仅用于图片签名，后端环境文件不再需要 `API_PUBLIC_BASE_URL`。
Bucket、地域与密钥等完整配置见 [配置与日志](configuration.md)。

## 方案列表

`GET /v1/solutions`，公开，无需登录。

| 参数 | 规则 |
| --- | --- |
| `room` | 可选：`LIVING_ROOM`、`BEDROOM`、`BALCONY` |
| `need` | 可选：`QUIET`、`INSULATION`、`SAFETY`、`VENTILATION` |
| `keyword` | 可选，最长 120 字符，按名称、副标题、描述匹配；`%`、`_` 当普通文本处理 |
| `page` | 从 1 开始，默认 1 |
| `size` | 默认 20，范围 1～50 |

条件按交集过滤，仅返回上架方案，按运营排序再按 ID 稳定排序。`data` 格式：

```json
{
  "items": [{
    "id": "solution-living-panorama",
    "name": "客厅全景静音窗",
    "subtitle": "大视野与密封性能兼顾",
    "thumbnailUrl": "http://127.0.0.1:8000/catalog/living-after-thumb.jpg",
    "room": "LIVING_ROOM",
    "needs": ["QUIET", "INSULATION", "VENTILATION"],
    "features": ["窄边大视野", "多道密封", "内开内倒"],
    "pricingNote": "量尺后按实际尺寸报价",
    "available": true
  }],
  "page": 1,
  "size": 20,
  "total": 3,
  "hasNext": false
}
```

上例条目已省略其他两套方案；无匹配时 `items=[]`、`total=0`，不会返回假方案。

## 方案详情

`GET /v1/solutions/{solutionId}`，公开，无需登录。

返回 `id/name/subtitle/description/coverUrl/galleryUrls/room/needs/features/pricingNote/available`，以及：

```json
{
  "frameColors": [{"code": "SPACE_GRAY", "label": "太空灰", "colorHex": "#454b48", "available": true}],
  "glassOptions": [{"code": "DOUBLE_HOLLOW", "label": "双层中空玻璃", "available": true}],
  "defaultFrameColorCode": "SPACE_GRAY",
  "defaultGlassCode": "DOUBLE_HOLLOW"
}
```

数组保留配置顺序；示例仅展示部分选项。不存在或下架返回 HTTP 404 / `RESOURCE_NOT_FOUND`。
配置项以稳定 `code` 标识；后续生成接口仍需再次验证方案与配置可用性。

## 公开样例

`GET /v1/sample-scenes`，公开，无需登录，返回 `data.items`。

每项包含 `id/name/sceneType/thumbnailUrl/width/height/defaultSelection`。初始三个样例使用小程序
客厅、卧室、阳台的原有场景图，尺寸均为实际 810 × 1215；默认选区为：

```json
{"x": 0.12, "y": 0.12, "width": 0.76, "height": 0.7}
```

样例读取本身不创建用户照片或生成任务。公开图片路径 `/catalog/*` 与用户私有照片目录完全分开。

## 上传照片

`POST /v1/photos`，需要 `Authorization: Bearer <accessToken>`。
请求使用 `multipart/form-data`：`file` 为照片，`purpose` 必填且固定为 `WINDOW_PREVIEW`。

服务器根据实际文件内容解码 JPEG/PNG/HEIC，不根据扩展名或客户端 Content-Type 判定可信格式。
先限制上传字节和像素，再纠正 EXIF 方向、移除 EXIF/GPS 等元数据，统一存为 JPEG，并保存缩略图。
透明区域合成为白色背景。不支持的格式返回 415；损坏/空图片返回 422；超限返回 413。

成功返回 HTTP 200，`data`：

```json
{
  "photoId": "photo_0123456789abcdef0123456789abcdef",
  "width": 810,
  "height": 1215,
  "mimeType": "image/jpeg",
  "sizeBytes": 165000,
  "previewUrl": "http://127.0.0.1:8000/v1/photos/photo_0123456789abcdef0123456789abcdef/preview?expires=...&signature=...",
  "previewUrlExpiresAt": "2026-09-07T04:00:00Z",
  "createdAt": "2026-09-07T03:00:00Z"
}
```

以上是结构示例，真实尺寸、大小、ID 和时间按实际上传返回。`width/height/sizeBytes` 是规范化 JPEG 的值，
前端应以这些尺寸和处理后的方向为框选基准，后续业务提交 `photoId`。

原图和缩略图全部写入、预览地址签发及数据库提交成功后才返回成功；失败会回滚数据库并尝试删除已写对象。
若存储本身同时无法清理，则记录照片 ID 的安全错误摘要供排查，不能视为已完成物理清理。

## 删除照片

### 按账户找回上传照片

`GET /v1/photos?size=50&cursor=...` 需要 Bearer 登录，返回：

```json
{
  "items": [{"photoId": "photo_0123456789abcdef0123456789abcdef", "createdAt": "2026-10-22T03:00:00Z", "deleted": false}],
  "nextCursor": null
}
```

只列当前账户用途为 `WINDOW_PREVIEW` 的现场上传，不包含其他用户照片、生成输入和效果图，
不返回存储 key 或图片签名。`size` 为 1～100；后续页携带上一页的 `nextCursor`，为空即结束。
按不可变照片 ID 升序分页，逐页删除不会产生 offset 位移。列表包含已逻辑删除的墓碑，
让客户端能重新执行删除，补偿之前失败的物理清理；因此 `deleted=true` 不代表底层对象删除已经确认。

该接口可以找回上传响应或设备索引丢失的资源。前端必须用明确的账户级清理入口告知覆盖所有设备，
不能静默扩大原“本设备清理”的范围。接口本身不删除照片，不需要数据库结构迁移。

### 删除单张上传照片

`DELETE /v1/photos/{photoId}`，需要 Bearer 登录。只能删除本人照片；其他用户资源返回
403 / `RESOURCE_FORBIDDEN`，不存在返回 404 / `RESOURCE_NOT_FOUND`。

删除先保存逻辑标记，再清理原图和缩略图。重复删除本人已删除照片仍返回成功：

```json
{"code":"OK","message":"success","data":null,"requestId":"req_..."}
```

存储清理失败返回 503，保留删除标记以允许重试。本地旧签名不能再读取逻辑删除的照片。
S3 链接的访问是否已撤销取决于对象删除是否成功；清理失败时旧签名最多仍可用到其到期时间。
生成中的任务引用源照片时返回 `409 / PHOTO_IN_USE`；已完成结果保存独立原图快照，删除源照片不影响回看。

## 本地存储的预览支持路由

`GET /v1/photos/{photoId}/preview?expires=<unixSeconds>&signature=<signature>` 返回 JPEG 文件，
属于照片上传的支持路由，不是 JSON 业务信封。图片组件无需 Bearer，必须持有服务器签发的有效链接。
响应使用 `Cache-Control: private, no-store`，签名无效/过期返回 403；删除后返回 404。
S3 模式使用存储服务自己的预签名 URL，本地预览路由返回 404。

## 错误与验证范围

| HTTP | 错误码 | 含义 |
| --- | --- | --- |
| 400 | `INVALID_ARGUMENT` | 筛选、分页、表单参数无效 |
| 401 | `UNAUTHORIZED` | 缺少登录、令牌失效或账号不可用 |
| 403 | `RESOURCE_FORBIDDEN` | 试图删除其他用户照片 |
| 403 | `PHOTO_PREVIEW_EXPIRED` | 预览签名无效或过期 |
| 404 | `RESOURCE_NOT_FOUND` | 不存在/下架方案或不存在/已删除照片 |
| 413 | `PHOTO_TOO_LARGE` | 文件、整体请求或图像像素超限 |
| 415 | `PHOTO_UNSUPPORTED` | 不支持的图片格式 |
| 422 | `PHOTO_DECODE_FAILED` | 空文件或图片损坏 |
| 503 | `PHOTO_STORAGE_UNAVAILABLE` | 私有存储读写/签名暂时失败 |

自动化验证使用临时 SQLite、真实图片编解码、本地私有文件和 S3 SDK 替身；真实 MySQL/S3 及微信真机
需在各自环境联调。启用云端照片后，小程序原有“照片不上传、预约仅本地”等文案应随实际功能同步更新。

实现参考：[FastAPI 文件上传](https://fastapi.tiangolo.com/tutorial/request-files/)、
[Pillow 图像处理](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html)、
[S3 预签名 URL](https://docs.aws.amazon.com/boto3/latest/guide/s3-presigned-urls.html)。
