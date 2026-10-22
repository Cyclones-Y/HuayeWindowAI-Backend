"""只解析临时合成配置，防止 Compose 插值与 API/worker 注入不同环境。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("app_env", ["development", "production"])
def test_compose_uses_selected_environment_for_interpolation_and_containers(tmp_path: Path, app_env: str) -> None:
    """CLI 的 --env-file 负责插值，服务 env_file 负责注入，二者必须选中同一文件。"""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI 不可用，无法执行 Compose config 验证")

    compose_path = Path(__file__).resolve().parents[1] / "compose.yaml"
    shutil.copyfile(compose_path, tmp_path / "compose.yaml")
    # 基础文件故意保留冲突值，捕获误读旧 .env 或跨环境读取的回归；不会读取开发者真实文件。
    (tmp_path / ".env").write_text("WECHAT_APP_ID=wx_legacy_base\n", encoding="utf-8")
    for environment in ("development", "production"):
        (tmp_path / f".env.{environment}").write_text(
            f"APP_ENV={environment}\n"
            f"MYSQL_DATABASE={environment}_database\n"
            f"MYSQL_USER={environment}_user\n"
            f"MYSQL_PASSWORD={environment}_password\n"
            "MYSQL_ROOT_PASSWORD=synthetic_root_password\n"
            f"WECHAT_APP_ID=wx_{environment}\n"
            "DATABASE_URL=mysql+asyncmy://localhost/must_be_overridden\n"
            "REDIS_URL=redis://localhost:6379/9\n",
            encoding="utf-8",
        )

    # 调用者的 APP_ENV、Compose 设置或 MYSQL_* 均不能污染此隔离场景。
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key != "APP_ENV" and not key.startswith(("MYSQL_", "COMPOSE_"))
    }
    result = subprocess.run(  # noqa: S603 - 固定 Docker 子命令，只解析临时合成配置，不启动容器。
        [docker, "compose", "--env-file", f".env.{app_env}", "--profile", "render", "config", "--format", "json"],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 and "is not a docker command" in result.stderr:
        pytest.skip("Docker Compose 插件不可用")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]

    assert services["mysql"]["environment"]["MYSQL_DATABASE"] == f"{app_env}_database"
    for service_name in ("api", "render-worker"):
        environment = services[service_name]["environment"]
        assert environment["APP_ENV"] == app_env
        assert environment["WECHAT_APP_ID"] == f"wx_{app_env}"
        assert environment["DATABASE_URL"] == (
            f"mysql+asyncmy://{app_env}_user:{app_env}_password@mysql:3306/{app_env}_database?charset=utf8mb4"
        )
        assert environment["REDIS_URL"] == "redis://redis:6379/0"
