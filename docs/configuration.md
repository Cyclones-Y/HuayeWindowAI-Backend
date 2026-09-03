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

`plain` 控制台格式与 RuoYi-Vue3-FastAPI 保持相同顺序：时间、`traceId`、`spanId`、`requestId`、
`workerId`、日志级别、调用位置和消息。启动成功后，应用地址和 Swagger 地址采用带图标的多行展示，不再把配置对象
作为 JSON 拼接在日志尾部。

## 启动时应看到的信息

直接运行 `src/huaye_window_ai/main.py` 后，控制台会依次显示：

1. 应用名称、版本、环境、Python 版本和监听地址；
2. MySQL、Redis 的连接检查结果；
3. 本机与局域网应用地址、Swagger 地址；
4. 日志落盘目录；
5. 每次 HTTP 请求的 `requestId`、状态码和耗时。

若端口已被占用，Uvicorn 错误也会同时写入控制台和当天的 `error.log`。
