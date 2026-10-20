"""deploy.yml loading. Relative paths in the file resolve against the file's own directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import DeployError

DEFAULT_CONFIG = "deploy.yml"


@dataclass(frozen=True)
class Config:
    path: Path

    host: str
    user: str
    port: int
    key_file: str | None
    accept_new_host_key: bool
    remote_dir: str
    project_name: str

    registry_server: str
    registry_username: str
    image: str

    git_branch: str
    git_remote: str

    build_context: Path
    dockerfile: str
    platform: str
    compose_file: Path
    env_file: Path
    migrations_dir: Path

    api_service: str
    worker_service: str
    db_container: str
    redis_container: str
    db_user: str
    db_name: str

    health_url: str
    health_timeout: int
    drain_seconds: int
    settle_seconds: int
    worker_stable_seconds: int
    keep_images: int
    keep_backups: int
    lock_timeout_ms: int
    min_free_gb: int


def _need(section: dict, key: str, where: str):
    value = section.get(key)
    if value in (None, ""):
        raise DeployError(f"deploy.yml: '{where}.{key}' is required")
    return value


def load(path: str | Path | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG).resolve()
    if not path.is_file():
        raise DeployError(f"{path} not found. Run `hoist init` in the backend directory first.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise DeployError(f"{path.name} is not valid YAML: {exc}") from exc

    base = path.parent
    server = raw.get("server") or {}
    registry = raw.get("registry") or {}
    build = raw.get("build") or {}
    git = raw.get("git") or {}
    services = raw.get("services") or {}
    database = raw.get("database") or {}
    health = raw.get("health") or {}
    tuning = raw.get("deploy") or {}

    def rel(value: str) -> Path:
        return (base / value).resolve()

    return Config(
        path=path,
        host=_need(server, "host", "server"),
        user=_need(server, "user", "server"),
        port=int(server.get("port", 22)),
        key_file=server.get("key_file"),
        accept_new_host_key=bool(server.get("accept_new_host_key", False)),
        remote_dir=str(_need(server, "remote_dir", "server")).rstrip("/"),
        project_name=_need(raw, "project_name", "(root)"),
        registry_server=registry.get("server", "ghcr.io"),
        registry_username=_need(registry, "username", "registry"),
        image=_need(registry, "image", "registry"),
        git_branch=str(git.get("branch", "production")),
        git_remote=str(git.get("remote", "origin")),
        build_context=rel(build.get("context", ".")),
        dockerfile=build.get("dockerfile", "Dockerfile"),
        platform=build.get("platform", "linux/amd64"),
        compose_file=rel(raw.get("compose_file", "docker-compose.prod.yml")),
        env_file=rel(raw.get("env_file", ".env.production")),
        migrations_dir=rel(raw.get("migrations_dir", "alembic/versions")),
        api_service=services.get("api", "api"),
        worker_service=services.get("worker", "worker"),
        db_container=services.get("db_container", "homealert_db"),
        redis_container=services.get("redis_container", "homealert_redis"),
        db_user=database.get("user", "homealert"),
        db_name=database.get("name", "homealert"),
        health_url=health.get("url", ""),
        health_timeout=int(health.get("timeout", 150)),
        drain_seconds=int(tuning.get("drain_seconds", 30)),
        settle_seconds=int(tuning.get("settle_seconds", 5)),
        worker_stable_seconds=int(tuning.get("worker_stable_seconds", 10)),
        keep_images=int(tuning.get("keep_images", 5)),
        keep_backups=int(tuning.get("keep_backups", 7)),
        lock_timeout_ms=int(tuning.get("lock_timeout_ms", 5000)),
        min_free_gb=int(tuning.get("min_free_gb", 3)),
    )


TEMPLATE = """\
# hoist config. Paths are relative to this file.
project_name: {project_name}      # docker compose project on the server (must match the running stack)

server:
  host: {host}
  user: {user}
  port: 22
  key_file: ~/.ssh/id_ed25519      # omit to use ssh-agent / default keys
  accept_new_host_key: false       # true = trust an unknown host key on first connect
  remote_dir: {remote_dir}         # holds compose file, .env.production, backups, release log

registry:
  server: ghcr.io
  username: {registry_username}
  image: {image}                   # no tag; hoist tags with the git SHA

git:
  branch: production               # only the committed, pushed tip of this branch is ever deployed
  remote: origin

build:
  context: .
  dockerfile: Dockerfile
  platform: linux/amd64

compose_file: docker-compose.prod.yml   # read from the branch, not the working tree
env_file: .env.production          # local copy; pushed with `hoist env push`
migrations_dir: alembic/versions        # read from the branch, not the working tree

services:
  api: api
  worker: worker
  db_container: homealert_db
  redis_container: homealert_redis

database:
  user: homealert
  name: homealert

health:
  url: {health_url}
  timeout: 150

deploy:
  drain_seconds: 30        # old container gets this long to finish in-flight requests
  settle_seconds: 5        # pause after the new one is healthy so Traefik routes to it
  worker_stable_seconds: 10
  keep_images: 5
  keep_backups: 7
  lock_timeout_ms: 5000    # migrations abort instead of queueing behind a long query
  min_free_gb: 3
"""
