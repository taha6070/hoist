from __future__ import annotations

from pathlib import Path

import pytest

from hoist import config
from hoist.errors import DeployError


def render(tmp: Path) -> Path:
    path = tmp / "deploy.yml"
    path.write_text(config.TEMPLATE.format(
        project_name="backend", host="1.2.3.4", user="deploy", remote_dir="/opt/homealert",
        registry_username="me", image="ghcr.io/me/homealert-api", health_url="https://x/health"), encoding="utf-8")
    return path


def test_template_round_trips(tmp_path: Path) -> None:
    cfg = config.load(render(tmp_path))
    assert (cfg.host, cfg.project_name, cfg.image) == ("1.2.3.4", "backend", "ghcr.io/me/homealert-api")
    assert cfg.compose_file == (tmp_path / "docker-compose.prod.yml").resolve()
    assert cfg.lock_timeout_ms == 5000 and cfg.drain_seconds == 30
    assert (cfg.git_branch, cfg.git_remote) == ("production", "origin")


def test_missing_file_points_at_init(tmp_path: Path) -> None:
    with pytest.raises(DeployError, match="hoist init"):
        config.load(tmp_path / "nope.yml")


def test_missing_required_key(tmp_path: Path) -> None:
    path = tmp_path / "deploy.yml"
    path.write_text("project_name: p\nserver: {user: u, remote_dir: /x}\nregistry: {username: me, image: i}\n")
    with pytest.raises(DeployError, match="server.host"):
        config.load(path)
