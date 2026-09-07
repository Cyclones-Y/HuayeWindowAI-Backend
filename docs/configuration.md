# 配置与日志

后端从项目根目录的 `.env` 读取基础配置，因此从 PyCharm、命令行或源码子目录启动时行为一致。
首次运行请复制 `.env.example` 为 `.env`，只在未提交的环境文件中填写真实密码和微信密钥。

## 配置优先级

程序会根据 `APP_ENV` 继续加载 `.env.development`、`.env.test` 或 `.env.production`，环境文件覆盖基础 `.env`，
进程环境变量优先级最高。环境文件均已加入 `.gitignore`，可以只在本机或部署平台保存。

MySQL 和 Redis 默认采用分项配置，便于阅读和排错；容器平台需要注入完整连接串时，
可以分别设置 `DATABASE_URL` 和 `REDIS_URL`，它们会覆盖对应的 `DB_*` 与 `REDIS_*` 字段。

## 应用配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_ENV` | `development` | 运行环境：`development`、`test`、`production` |
| `APP_NAME` | `华业门窗 AI 小程序后端` | 日志与 OpenAPI 中显示的服务名 |
| `APP_VERSION` | `0.1.0` | 当前服务版本 |
| `APP_HOST` | `127.0.0.1` | 真机局域网联调应设为 `0.0.0.0` |
| `APP_PORT` | `8000` | HTTP 监听端口 |
| `APP_DOCS_ENABLED` | `true` | 是否启用 `/docs`；生产环境必须关闭 |
| `APP_STARTUP_CHECKS_ENABLED` | `true` | 启动时是否主动检查 MySQL 与 Redis |
| `API_PREFIX` | `/v1` | 所有业务接口统一前缀，可改 `/api/v1`；末尾 `/` 自动去除，空值表示不加前缀 |
| `API_PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 客户端可访问的地址，不含业务前缀，用于本地照片签名预览 |
| `CATALOG_PUBLIC_BASE_URL` | 空值 | 空值派生为 `API_PUBLIC_BASE_URL/catalog`；可配置已同步目录素材的 CDN 地址 |

例如 `.env` 配置：

```dotenv
API_PREFIX=/api/v1
API_PUBLIC_BASE_URL=http://192.168.1.100:8000
```

重启后业务接口为 `/api/v1/auth/wechat/login`、`/api/v1/solutions`、`/api/v1/photos` 等，
Swagger 自动显示这些真实路径。前端请求中的 `/v1` 需要同步改为 `/api/v1`；服务不同时保留两套别名。
`/health/live`、`/health/ready`、`/docs`、`/openapi.json`、`/catalog/*` 不随前缀改变。
若 `.env.development` 等环境配置也定义同名字段，它们仍按原有优先级覆盖基础 `.env`。

## 照片配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PHOTO_STORAGE_BACKEND` | `local` | `local` 私有文件目录，或 `s3` 私有对象存储 |
| `PHOTO_LOCAL_DIRECTORY` | `data/photos` | 相对目录按项目根目录解析；不会作为静态目录公开 |
| `PHOTO_MAX_BYTES` | `10485760` | 上传文件上限 10 MiB；请求体另允许 64 KiB multipart 开销 |
| `PHOTO_MAX_PIXELS` | `40000000` | 解码前校验像素总数，限制超大图内存消耗 |
| `PHOTO_THUMBNAIL_MAX_EDGE` | `512` | 缩略图最长边，保持比例、不放大小图 |
| `PHOTO_PREVIEW_TTL_SECONDS` | `3600` | 预览签名有效期，允许 60～86400 秒 |
| `PHOTO_URL_SIGNING_KEY` | 空值 | local 的 HMAC 密钥；开发留空使用随机进程密钥，生产必须至少 32 字符 |
| `PHOTO_S3_BUCKET` | 空值 | s3 模式必填，Bucket 必须预先配置为私有 |
| `PHOTO_S3_ENDPOINT_URL` | 未设置 | S3 兼容服务端点；标准 AWS 环境可不配置 |
| `PHOTO_S3_REGION` | 未设置 | 对象存储区域；未设置时沿用 SDK 配置 |
| `PHOTO_S3_ACCESS_KEY_ID` | 空值 | 与 secret 成对配置，或均留空使用 SDK 凭据链 |
| `PHOTO_S3_SECRET_ACCESS_KEY` | 空值 | 只在未提交环境文件或密钥管理中填写 |

生产环境的公网资源地址及显式 S3 端点必须使用 HTTPS。本地模式多进程部署时各进程必须使用相同
签名密钥和共享持久化目录；开发随机密钥在进程重启后会使旧预览链接失效。
Compose 已挂载 `./data/photos:/app/data/photos`；如果改存储目录，需要同步修改持久卷。

切换存储后端、目录或 Bucket 前应先迁移已有对象。数据库保存对象键，直接切换配置不会自动搬迁图片。
S3 开启版本保留时，删除接口移除当前可访问对象，历史版本仍按 Bucket 生命周期规则清理。
第一批接口只删除照片；后续任务和结果引用照片时，需要接入引用保留规则。

## MySQL 与 Redis

MySQL 使用 `DB_HOST`、`DB_PORT`、`DB_USERNAME`、`DB_PASSWORD`、`DB_DATABASE` 和 `DB_ECHO`。
Redis 使用 `REDIS_HOST`、`REDIS_PORT`、`REDIS_USERNAME`、`REDIS_PASSWORD` 和 `REDIS_DATABASE`。

`DB_ECHO=true` 会输出 SQL 语句，只适合短时间本地排错，不建议在生产环境开启。

## 日志配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_LOG_LEVEL` | `INFO` | 最低日志级别 |
| `LOG_CONSOLE_ENABLED` | `true` | 输出到控制台 |
| `LOG_CONSOLE_FORMAT` | `plain` | 控制台格式：`plain` 或 `json` |
| `LOG_FILE_ENABLED` | `true` | 写入滚动日志文件 |
| `LOG_FILE_FORMAT` | `json` | 文件格式：`plain` 或 `json` |
| `LOG_DIRECTORY` | `logs` | 日志目录，默认位于项目根目录 |
| `LOG_ROTATION` | `50 MB` | 单个日志文件达到该大小后滚动 |
| `LOG_RETENTION` | `30 days` | 滚动日志保留时间 |
| `LOG_COMPRESSION` | `zip` | 历史日志压缩格式 |
| `LOG_INSTANCE_ID` | `default` | 部署实例标识，供多实例日志区分 |
| `LOG_ACCESS_ENABLED` | `true` | 是否记录请求方法、路径、状态码和耗时 |

日志文件包括：

- `logs/年/月/日/info.log`：应用、启动、请求和依赖检查等 `INFO` 日志；
- `logs/年/月/日/error.log`：`WARNING` 及以上日志，便于快速定位故障。

请求日志不会记录查询串、请求体、Bearer Token、微信临时码或手机号。结构化扩展字段中的敏感键会在写入前统一替换为
`[REDACTED]`。

`plain` 控制台格式保留 RuoYi-Vue3-FastAPI 的颜色和启动摘要，但针对当前单体服务精简为时间、级别、短
`requestId` 和消息；代码位置只在 `WARNING` 及以上级别显示。JSON 文件保存完整 `requestId`，访问字段只存一份，
不再重复写入 `extra`。失败请求还会记录业务错误码和安全的错误原因，例如
`WECHAT_CODE_INVALID | 微信登录凭证已失效，请重试`。

`/docs`、`/openapi.json` 和 `/health/live` 的成功访问默认不记录；失败访问仍正常记录。启动成功后，应用地址和
Swagger 地址采用带图标的多行展示。

## 启动时应看到的信息

直接运行 `src/huaye_window_ai/main.py` 后，控制台会依次显示：

1. 应用名称、版本、环境、Python 版本和监听地址；
2. MySQL、Redis 的连接检查结果；
3. 本机与局域网应用地址、Swagger 地址；
4. 日志落盘目录；
5. 每次 HTTP 请求的 `requestId`、状态码和耗时。

若端口已被占用，Uvicorn 错误也会同时写入控制台和当天的 `error.log`。
