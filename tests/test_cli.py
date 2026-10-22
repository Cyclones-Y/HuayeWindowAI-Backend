"""命令行必须在导入服务前确定环境，验证时不启动网络服务或写入数据库。

Python 子进程与捕获端统一使用 UTF-8，避免 Windows 的 GBK 解码与调用者的输出编码冲突。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize(
    ("command", "replace"), [("api", False), ("worker", False), ("migrate", False), ("seed", False), ("seed", True)]
)
def test_cli_selects_environment_before_starting_command(
    tmp_path: Path, environment: str, command: str, replace: bool
) -> None:
    """用真实模块入口验证导入时机，只替换阻塞/写入边界，不访问实际配置和服务。"""
    for mode in ("development", "production"):
        (tmp_path / f".env.{mode}").write_text(
            f"APP_ENV={mode}\nDB_HOST={mode}-db\nWECHAT_APP_ID=wx_synthetic\nWECHAT_APP_SECRET=fake\n"
            "APP_DOCS_ENABLED=false\nAPI_PUBLIC_BASE_URL=https://synthetic.example.test\n"
            "PHOTO_URL_SIGNING_KEY=synthetic-signing-key-longer-than-32-characters\n",
            encoding="utf-8",
        )
    # 父进程故意选相反环境；命令参数必须胜出。仅输出合成环境名和主机名。
    child_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("APP_", "DB_", "DATABASE_", "PHOTO_", "WECHAT_", "ARK_", "RENDER_", "LOG_"))
    }
    child_env["APP_ENV"] = "production" if environment == "development" else "development"
    child_env["PYTHONIOENCODING"] = "utf-8"
    probe = r"""
import asyncio
import json
import runpy
import sys
from pathlib import Path
import uvicorn
from alembic import command
from huaye_window_ai.core import config

config._PROJECT_ROOT = Path(sys.argv.pop(1))
def report():
    settings = config.Settings()
    database = config.DatabaseSettings()
    print(json.dumps({"environment": settings.app_env, "host": database.db_host}))
def run_api(app, **kwargs):
    report()
def run_async(coroutine):
    if coroutine.cr_code.co_name == "run":
        assert coroutine.cr_frame.f_locals["replace"] == ("--replace" in original_arguments)
    coroutine.close()
    report()
def migrate(configuration, revision):
    assert revision == "head"
    report()
uvicorn.run = run_api
asyncio.run = run_async
command.upgrade = migrate
original_arguments = list(sys.argv)
runpy.run_module("huaye_window_ai", run_name="__main__")
"""
    # api 故意省略，验证无子命令时确实启动 API；seed 同时覆盖默认补齐与显式覆盖模式。
    command_arguments = [] if command == "api" else [command]
    if replace:
        command_arguments.append("--replace")
    result = subprocess.run(  # noqa: S603 - 固定 Python 探针，只拦截服务/写入边界。
        [sys.executable, "-c", probe, str(tmp_path), "--env", environment, *command_arguments],
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"environment": environment, "host": f"{environment}-db"}


def test_cli_help_does_not_require_configuration(tmp_path: Path) -> None:
    """未配置密钥时也应能查看帮助；帮助不能导入会立即创建应用的 main 模块。"""
    result = subprocess.run(  # noqa: S603 - 仅运行入口帮助，不启动服务。
        [sys.executable, "-m", "huaye_window_ai", "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--env" in result.stdout


def test_cli_rejects_invalid_environment() -> None:
    """拼错环境应在读取文件和连接数据库之前明确失败。"""
    result = subprocess.run(  # noqa: S603 - 非法参数必须直接退出。
        [sys.executable, "-m", "huaye_window_ai", "--env", "prod"],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_cli_rejects_replace_for_api() -> None:
    """覆盖种子的开关不能误用到默认 API 命令后被静默忽略。"""
    result = subprocess.run(  # noqa: S603 - 参数错误应直接退出，不导入服务。
        [sys.executable, "-m", "huaye_window_ai", "--env", "development", "--replace"],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "seed" in result.stderr
