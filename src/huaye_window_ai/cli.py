"""统一命令入口：先选择配置环境，再导入 API、worker 或执行数据库工具。"""

from __future__ import annotations

import argparse
import asyncio
import os
import runpy
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> None:
    """解析一次启动命令；环境参数覆盖父进程选择，数据库命令仅在显式指定时执行。"""

    parser = argparse.ArgumentParser(description="选择开发或生产配置并启动华业门窗后端")
    parser.add_argument("--env", choices=("development", "production"), help="配置环境；未传时沿用 APP_ENV")
    parser.add_argument("command", nargs="?", choices=("api", "worker", "migrate", "seed"), default="api")
    parser.add_argument("--replace", action="store_true", help="仅 seed 使用：覆盖已有种子项")
    arguments = parser.parse_args(argv)
    if arguments.replace and arguments.command != "seed":
        parser.error("--replace 仅适用于 seed 命令")
    if arguments.env is not None:
        os.environ["APP_ENV"] = arguments.env

    # main 模块导入时即创建 ASGI app，因此必须在选择环境之后导入；--help 也无需配置密钥。
    from huaye_window_ai.core.config import get_settings

    get_settings.cache_clear()
    if arguments.command == "api":
        from huaye_window_ai.main import main as run_api

        run_api()
    elif arguments.command == "worker":
        from huaye_window_ai.worker import main as run_worker

        asyncio.run(run_worker())
    elif arguments.command == "migrate":
        from alembic.config import Config

        from alembic import command

        # 迁移目录使用绝对路径，避免从 IDE 或源码子目录启动时误找同名目录。
        configuration = Config(str(_PROJECT_ROOT / "alembic.ini"))
        configuration.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
        command.upgrade(configuration, "head")
    else:
        # 复用原有 seed 脚本的事务与覆盖规则，只转发它支持的参数，并恢复调用方 argv。
        script = _PROJECT_ROOT / "scripts" / "seed_catalog.py"
        previous_argv = sys.argv
        try:
            sys.argv = [str(script), *(["--replace"] if arguments.replace else [])]
            runpy.run_path(str(script), run_name="__main__")
        finally:
            sys.argv = previous_argv
