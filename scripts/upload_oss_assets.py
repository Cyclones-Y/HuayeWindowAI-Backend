"""生成 OSS 迁移计划；仅 --upload 才写云端，默认不连接 OSS 或写数据库。"""

from __future__ import annotations

import argparse
import asyncio
from importlib.resources import files
from pathlib import Path

from huaye_window_ai.core.config import Settings
from huaye_window_ai.integrations.oss_migration import catalog_plan, local_photo_plan, upload_verified
from huaye_window_ai.integrations.oss_storage import OssPhotoStorage


async def run(*, upload: bool, include_local_photos: bool, catalog_directory: Path | None) -> None:
    """先完整检查本地计划，再按项上传与回读；切换存储、迁移数据库均不自动执行。"""
    settings = Settings()
    root = catalog_directory or Path(str(files("huaye_window_ai").joinpath("data/public/catalog")))
    plan = catalog_plan(root, settings.catalog_oss_prefix)
    catalog_count = len(plan)
    if include_local_photos:
        plan.extend(await local_photo_plan(settings))
    print(f"迁移计划：目录图片 {catalog_count} 个，账户图片 {len(plan) - catalog_count} 个")
    if not upload:
        print("仅检查计划，未上传。填写 OSS 密钥后加 --upload 才执行；已有本地照片请加 --include-local-photos。")
        return
    # 可在当前仍使用 local 时先搬文件；复用完整 OSS 配置校验，防止空凭据写入。
    configured = Settings.model_validate({**settings.model_dump(), "photo_storage_backend": "oss"})
    storage = OssPhotoStorage(configured)
    try:
        for index, (key, source) in enumerate(plan, start=1):
            await upload_verified(storage, key, source)
            # 不打印私有对象路径、签名链接或 SDK 原始响应。
            print(f"上传并校验完成：{index}/{len(plan)}")
    finally:
        await storage.aclose()
    print("迁移完成；数据库路径未改变。确认 API 与 worker 均切换 PHOTO_STORAGE_BACKEND=oss 后再重启。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="上传方案素材及可选的有效本地账户照片至私有 OSS")
    parser.add_argument("--upload", action="store_true", help="实际上传；默认仅检查本地计划")
    parser.add_argument("--include-local-photos", action="store_true", help="查询数据库并迁移未删除的本地照片")
    parser.add_argument("--catalog-directory", type=Path, help="自定义目录素材根目录，默认使用随包图片")
    arguments = parser.parse_args()
    try:
        asyncio.run(
            run(
                upload=arguments.upload,
                include_local_photos=arguments.include_local_photos,
                catalog_directory=arguments.catalog_directory,
            )
        )
    except Exception as error:
        # 配置校验和 SDK 异常可能包含凭据；只显示安全错误类别，详情由本地测试定位。
        raise SystemExit(f"迁移失败（{type(error).__name__}），请核对配置、源文件与权限；未自动切换存储。") from None
