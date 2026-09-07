# HuayeWindowAI Backend

华业门窗 AI 效果预览小程序后端。当前提供微信手机号登录、方案目录、公开样例、私有照片上传和删除，以及 MySQL/Redis 健康检查。AI 生成、效果历史和预约接口尚未接入。

## 技术栈

- Python 3.12、uv、FastAPI、Pydantic v2
- SQLAlchemy 2 Async、Alembic、MySQL 8.4
- Redis 随机会话与微信全局 `access_token` 缓存
- pytest、ruff、mypy

## 本地启动

1. 复制 `.env.example` 为 `.env`，填写本地 MySQL/Redis 和微信 AppID/AppSecret。不要提交 `.env`。
2. 创建数据库和最小权限账号。数据库应使用 `utf8mb4`，应用连接的会话时区会固定为 UTC。
3. 安装依赖并执行迁移：

```powershell
uv sync --all-groups
uv run alembic upgrade head
uv run python scripts/seed_catalog.py
```

4. 启动服务：

```powershell
uv run python src\huaye_window_ai\main.py
```

也可以在 PyCharm 中直接运行 `src/huaye_window_ai/main.py`；该入口会读取 `.env` 中的
`APP_HOST`、`APP_PORT` 和 `APP_LOG_LEVEL`。IDE 直接运行模式不启用自动重载，便于调试器在同一进程中命中断点。

开发环境可访问 `http://127.0.0.1:8000/docs`。探针为 `/health/live` 与 `/health/ready`。
完整环境变量和日志说明见 [配置与日志](docs/configuration.md)。
第一批接口的参数、响应、图片存储与联调步骤见 [方案与照片接口](docs/api-first-batch.md)。

业务路径统一读取 `.env` 的 `API_PREFIX`（默认 `/v1`，例如改为 `/api/v1`），修改后重启服务；
微信登录也会随此前缀调整。`/docs`、`/openapi.json`、`/health/*` 和公开目录图片 `/catalog/*` 保持固定地址。
`API_PUBLIC_BASE_URL` 填客户端实际能访问的域名或局域网地址，不包含 `API_PREFIX`，用于生成照片预览链接。

种子脚本默认只补缺失的三套方案和三个样例，不覆盖已有目录修改。未导入时目录查询返回空列表；
服务启动不会自行创建表或写入种子。初始目录及公开图片复用现有小程序素材，不代表真实 AI 生成结果。

启动后控制台会显示应用版本、运行环境、Python 版本、MySQL/Redis 检查结果、应用与 Swagger 访问地址。
运行日志按日期写入 `logs/年/月/日/info.log`，警告与错误另写入同目录的 `error.log`；两类文件均支持滚动、保留和压缩配置。
日志模块参考 [RuoYi-Vue3-FastAPI](https://github.com/insistence/RuoYi-Vue3-FastAPI) 的 Loguru 设计，并按本项目的请求链路和脱敏规则做了适配。

本机已有 MySQL/Redis 时直接使用 `3306/6379`。若改用 Compose，容器暴露到宿主机的端口为 `13306/16379`，避免冲突：

```powershell
docker compose up --build
```

## 微信登录契约

`POST /v1/auth/wechat/login` 接收 `wx.login` 的 `code`、持久化的 `deviceId`，以及首次登录时 `getPhoneNumber` 返回的 `phoneCode`。新身份未提供手机号授权时返回 `PHONE_AUTH_REQUIRED`；已绑定身份后仅用新的 `wx.login` code 即可静默登录。

后端不会提供注册、密码或短信验证码接口。手机号按用户要求直接存储在 MySQL 中，仅用于账号识别；日志和 Redis 不保存手机号，微信 `session_key` 不保存、不返回、不记录日志。

## 验证

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

真实手机号授权码只能由已认证小程序在用户点击授权按钮后产生。自动化测试使用微信服务替身；上线前仍需配置 HTTPS 业务域名并进行真机烟测。
