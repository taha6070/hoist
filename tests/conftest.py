from __future__ import annotations

from pathlib import Path

import pytest

from hoist.config import Config
from hoist.ssh import Result


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        path=tmp_path / "deploy.yml", host="h", user="u", port=22, key_file=None, accept_new_host_key=False,
        remote_dir="/opt/homealert", project_name="backend",
        registry_server="ghcr.io", registry_username="me", image="ghcr.io/me/homealert-api",
        git_branch="production", git_remote="origin",
        build_context=tmp_path, dockerfile="Dockerfile", platform="linux/amd64",
        compose_file=tmp_path / "docker-compose.prod.yml", env_file=tmp_path / ".env.production",
        migrations_dir=tmp_path / "versions",
        api_service="api", worker_service="worker", db_container="homealert_db",
        redis_container="homealert_redis", db_user="homealert", db_name="homealert",
        health_url="", health_timeout=9, drain_seconds=1, settle_seconds=0, worker_stable_seconds=2,
        keep_images=5, keep_backups=7, lock_timeout_ms=5000, min_free_gb=1,
    )


class FakeRemote:
    """Scripted Runner: the first rule whose substring is in the command answers it."""

    def __init__(self, rules: list[tuple[str, Result | list[Result]]] | None = None):
        self.rules = rules or []
        self.calls: list[str] = []
        self.files: dict[str, str] = {}

    def run(self, cmd, *, check=True, stream=False, stdin=None, timeout=None) -> Result:
        self.calls.append(cmd)
        for needle, answer in self.rules:
            if needle in cmd:
                if isinstance(answer, list):
                    res = answer.pop(0) if len(answer) > 1 else answer[0]
                else:
                    res = answer
                break
        else:
            res = Result(0, "", "")
        if check and res.code != 0:
            from hoist.errors import DeployError
            raise DeployError(f"failed: {cmd}")
        return res

    def put_text(self, path, content, mode=0o644):
        self.files[path] = content

    def ran(self, needle: str) -> bool:
        return any(needle in c for c in self.calls)
