# 方案、样例与照片接口

当前已实现微信登录之外的第一批 5 个业务接口。本文示例使用默认 `API_PREFIX=/v1`，实际路径以
当前实例 Swagger 为准。所有 JSON 响应沿用 `code/message/data/requestId`，ID 是字符串，时间使用 ISO 8601。

## 初始化与配置

先安装依赖，再在需要启用功能的数据库执行迁移和显式种子导入：

```powershell
uv sync --all-groups
uv run alembic upgrade head
uv run python scripts/seed_catalog.py
uv run python src/huaye_window_ai/main.py
```

迁移新增 `window_solutions`、`sample_scenes`、`photos` 三张表，不改已有账号数据。
导入脚本默认仅新增缺失项；`--replace` 才会覆盖已有种子方案的字段（包括上下架和排序），应按需使用。
当前三套初始目录及图片来自小程序现有素材，保留原方案 ID，方便前端接入。

在 `.env` 中配置统一前缀和客户端能够访问的地址：

```dotenv
API_PREFIX=/api/v1
API_PUBLIC_BASE_URL=http://192.168.1.100:8000
PHOTO_STORAGE_BACKEND=local
PHOTO_LOCAL_DIRECTORY=data/photos
```

此配置使登录变为 `POST /api/v1/auth/wechat/login`，方案变为 `GET /api/v1/solutions`。
地址示例需替换为实际主机地址；真机不能用 `127.0.0.1` 访问开发电脑。前端当前硬编码的 `/v1`
要同步调整，本次后端开发未切换前端 MockGateway。全部配置见 [配置与日志](configuration.md)。

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

`DELETE /v1/photos/{photoId}`，需要 Bearer 登录。只能删除本人照片；其他用户资源返回
403 / `RESOURCE_FORBIDDEN`，不存在返回 404 / `RESOURCE_NOT_FOUND`。

删除先保存逻辑标记，再清理原图和缩略图。重复删除本人已删除照片仍返回成功：

```json
{"code":"OK","message":"success","data":null,"requestId":"req_..."}
```

存储清理失败返回 503，保留删除标记以允许重试。本地旧签名不能再读取逻辑删除的照片。
S3 链接的访问是否已撤销取决于对象删除是否成功；清理失败时旧签名最多仍可用到其到期时间。
当前尚无生成任务引用照片，未来接入生成结果后需增加引用与保留策略。

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
