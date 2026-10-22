# OSS 接入与切换

使用阿里云 OSS Python SDK V2 和 V4 签名，通过已绑定域名访问私有 Bucket。
后端生成链接，小程序不持有 AccessKey。当前目标为 `huayewindowai` / `cn-beijing` /
`https://oss.zmintz.com`，方案与样例使用 `catalog/` 前缀，账户照片与生成图片使用 `photos/` 前缀。

## 配置与数据结构

开发、生产环境文件已按 `.env.example` 重新生成，统一配置 `PHOTO_STORAGE_BACKEND=oss`，
不再包含本地照片目录、HMAC 或 S3 配置。原有效密钥与连接信息保留，旧环境文件备份在
Git 忽略的 `data/oss-migration-backups/`。生产后端域名及数据库连接仍有待填写项，尚未部署生产服务。

`API_PUBLIC_BASE_URL` 已从模板与两份环境文件移除，OSS 模式启动不再要求该字段。
图片域名使用 `OSS_ENDPOINT`；后端对外地址在小程序与服务器反向代理中配置。

```dotenv
PHOTO_STORAGE_BACKEND=oss
OSS_BUCKET=huayewindowai
OSS_REGION=cn-beijing
OSS_ENDPOINT=https://oss.zmintz.com
OSS_ACCESS_KEY_ID=自行填写
OSS_ACCESS_KEY_SECRET=自行填写
CATALOG_OSS_PREFIX=catalog
```

两项密钥必须填写有效值，不要使用本文占位文字。RAM 权限限定到此 Bucket 的对象读取、写入和
删除即可，不需要修改 Bucket 权限、公开 ACL 或管理域名的权限。小程序仍向后端上传图片，
后端纠正方向、清除元数据后写入 OSS，访问凭据不返回前端。

`window_solutions` 的 `thumbnail_path`、`cover_path`、`gallery_paths` 及 `sample_scenes.thumbnail_path`
继续保存相对路径，例如 `living-after-thumb.jpg`，对应 OSS 键 `catalog/living-after-thumb.jpg`。
`photos.storage_key` / `thumbnail_key` 保留 `photos/<随机标识>/original.jpg` 等路径。
不新增表或批量写域名，接口字段仍为 `thumbnailUrl`、`coverUrl`、`previewUrl` 等。

oss 模式下目录和用户图片都返回 CNAME 限时签名链接，期限由 `PHOTO_PREVIEW_TTL_SECONDS` 控制，
默认一小时。目录免登录，但只签数据库中的目录图片路径；住宅照片及生成结果仍经过原有账户
鉴权及归属检查。签名不存库，不要去掉签名参数、替换签名域名或把临时链接写回数据库。
生成流程从同一 OSS 路径读取样例和方案参考图，不回退到旧的随包图片。

## 已完成的开发环境切换

2026-10-22，经用户确认上传9张目录图片，上传后全部从 OSS 回读且字节一致。
数据库中3套方案、3个样例共引用9张素材，全部已覆盖；当时没有历史账户照片或进行中任务。
开发 API 与 worker 已使用 OSS 配置重启，`/health/ready` 的 MySQL、Redis、worker 均通过。
通过实际目录接口取得9张 CNAME 签名图片，HTTPS 下载全部为200且与源文件一致；去掉签名为403。
未调用付费图片生成服务；上传照片、生成结果清理及手机微信访问仍需业务验收。

## 后续环境部署与新增素材

1. 从 `.env.example` 创建对应环境文件，填写密钥及后端域名、数据库和 Redis 信息，安装依赖：
   `uv sync --dev`。API 与 worker 必须选同一环境文件，生产须关闭文档与 SQL 调试输出。
2. 检查随包目录素材计划，默认不会写 OSS：

   ```powershell
   uv run python scripts/upload_oss_assets.py
   ```

3. 显式上传并逐文件回读校验：

   ```powershell
   uv run python scripts/upload_oss_assets.py --upload
   ```

   默认上传随包目录图片，用户照片与生成结果由后端直接写 OSS。
   如果方案素材另行维护，可通过 `--catalog-directory <素材根目录>` 指定实际文件夹，
   其相对路径必须与数据库一致。缺少源文件会停止，不能忽略错误后直接切换。
   写入禁止覆盖；重复执行时相同内容可复用，不同内容的同名对象会停止。
   脚本不修改数据库、不删除源文件，也不自动切换模式。
4. 配置和素材就绪后，启动对应环境的 API 与 worker，均保持 `PHOTO_STORAGE_BACKEND=oss`。
   确认进程环境变量没有覆盖所选配置文件；修改文件不会改变已运行进程加载的值。
5. 在微信公众平台将 `https://oss.zmintz.com` 配置为下载合法域名；后端域名仍用于请求和上传。
   结束并重新进入真机调试，验证方案、样例、现场照片、生成结果及删除。

新增方案先上传文件，再更新数据库路径，然后重新请求接口获取签名。
历史迁移工具保留 `--include-local-photos`，仅在存在旧照片时使用；迁移期间应停止 API/worker，
只读取未删除照片记录，不上传无登记残留。现有开发环境无需再执行历史照片迁移。
开启 OSS 版本保留时，历史对象版本按 Bucket 生命周期规则另行清理。

## 验收边界

本地测试使用合成凭据验证真实 SDK 的 CNAME/V4 签名，网络边界替身验证读写、隔离和迁移。
真实目录图片写入、回读和签名下载已经通过；其他业务路径、删除权限、Bucket 版本策略和微信真机仍待验收。
本接入不自动改 Bucket 权限、域名、微信后台或隐私备案；运营者需同步存储服务提供方说明。

参考：[官方 SDK 配置与 CNAME](https://help.aliyun.com/zh/oss/developer-reference/2-0-manual-preview-version/)、
[预签名下载](https://help.aliyun.com/zh/oss/user-guide/how-to-obtain-the-url-of-a-single-object-or-the-urls-of-multiple-objects/)。
