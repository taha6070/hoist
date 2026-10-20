"""Everything that runs on the server, expressed as commands over a Runner."""

from __future__ import annotations

import json
import shlex
import time
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen

from . import ui
from .config import Config
from .errors import DeployError
from .ssh import Runner

COMPOSE_NAME = "docker-compose.prod.yml"
ENV_NAME = ".env.production"
RELEASE_ENV = ".release.env"
RELEASE_LOG = "releases.jsonl"

_HEALTH_FMT = "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}"


def split_tag(image_ref: str) -> str:
    return image_ref.rsplit(":", 1)[-1] if ":" in image_ref.rsplit("/", 1)[-1] else ""


class Server:
    def __init__(self, cfg: Config, remote: Runner, sleep=time.sleep):
        self.cfg = cfg
        self.r = remote
        self._sleep = sleep
        self.dir = cfg.remote_dir

    # ── paths and compose ──────────────────────────────────────────────────
    def path(self, name: str) -> str:
        return f"{self.dir}/{name}"

    def compose(self, args: str, image: str | None = None) -> str:
        # A shell-level API_IMAGE beats the env file, so the new tag is used before it is recorded.
        prefix = f"API_IMAGE={shlex.quote(image)} " if image else ""
        return (
            f"cd {shlex.quote(self.dir)} && {prefix}docker compose -p {shlex.quote(self.cfg.project_name)} "
            f"-f {COMPOSE_NAME} --env-file {ENV_NAME} --env-file {RELEASE_ENV} {args}"
        )

    def sh(self, cmd: str, **kw):
        return self.r.run(cmd, **kw)

    # ── preflight ──────────────────────────────────────────────────────────
    def preflight(self) -> None:
        cfg = self.cfg
        self.sh("docker version --format '{{.Server.Version}}'")
        self.sh("docker compose version --short")
        if not self._exists(ENV_NAME):
            raise DeployError(f"{self.path(ENV_NAME)} is missing on the server. Run `hoist env push`.")
        self.sh(f"touch {self.path(RELEASE_ENV)}")
        for name in (cfg.db_container, cfg.redis_container):
            running = self.sh(f"docker ps --format '{{{{.Names}}}}' | grep -x {shlex.quote(name)} || true").out.strip()
            if not running:
                raise DeployError(f"container {name} is not running on the server")
        label = self.sh(
            f"docker inspect -f '{{{{index .Config.Labels \"com.docker.compose.project\"}}}}' {cfg.db_container}"
        ).out.strip()
        if label and label != cfg.project_name:
            raise DeployError(
                f"project_name is '{cfg.project_name}' but the running database belongs to compose project "
                f"'{label}'. A wrong name would mount empty volumes. Fix project_name in deploy.yml."
            )
        free_kb = int(self.sh(f"df -Pk {shlex.quote(self.dir)} | awk 'NR==2{{print $4}}'").out.strip() or 0)
        if free_kb < cfg.min_free_gb * 1024 * 1024:
            raise DeployError(f"only {free_kb // 1024 // 1024} GB free on the server (need {cfg.min_free_gb} GB)")

    def _exists(self, name: str) -> bool:
        return self.sh(f"test -f {self.path(name)}", check=False).code == 0

    # ── release state ──────────────────────────────────────────────────────
    def current_image(self) -> str | None:
        out = self.sh(f"grep '^API_IMAGE=' {self.path(RELEASE_ENV)} | tail -n1 || true").out.strip()
        return out.split("=", 1)[1] if "=" in out else None

    def releases(self) -> list[dict]:
        raw = self.sh(f"cat {self.path(RELEASE_LOG)} 2>/dev/null || true").out
        rows = []
        for line in raw.splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def record(self, image: str, **fields) -> dict:
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "image": image,
               "tag": split_tag(image), **fields}
        self.sh(f"printf '%s\\n' {shlex.quote(json.dumps(row))} >> {self.path(RELEASE_LOG)}")
        self.sh(f"printf 'API_IMAGE=%s\\n' {shlex.quote(image)} > {self.path(RELEASE_ENV)}")
        return row

    def db_revision(self) -> str | None:
        cfg = self.cfg
        proc = self.sh(
            f"docker exec {cfg.db_container} psql -U {cfg.db_user} -d {cfg.db_name} -tAc "
            "'select version_num from alembic_version'",
            check=False,
        )
        if proc.code != 0:
            if "does not exist" in proc.err:
                return None  # fresh database
            raise DeployError(f"cannot read the database revision: {proc.err.strip()[-300:]}")
        return proc.out.strip().splitlines()[0].strip() if proc.out.strip() else None

    # ── images ─────────────────────────────────────────────────────────────
    def pull(self, image: str) -> None:
        self.sh(f"docker pull {shlex.quote(image)}", stream=True, timeout=1800)

    def has_image(self, image: str) -> bool:
        return self.sh(f"docker image inspect {shlex.quote(image)} >/dev/null 2>&1", check=False).code == 0

    def adopt_legacy(self, service_ids: list[str]) -> str | None:
        """First deploy over a stack that was built on the server: tag its image so rollback works."""
        if not service_ids:
            return None
        image_id = self.sh(f"docker inspect -f '{{{{.Image}}}}' {service_ids[0]}").out.strip()
        legacy = f"{self.cfg.image}:legacy"
        self.sh(f"docker tag {image_id} {shlex.quote(legacy)}")
        return legacy

    def prune(self, keep: list[str]) -> None:
        cfg = self.cfg
        tags = self.sh(f"docker images {shlex.quote(cfg.image)} --format '{{{{.Tag}}}}'", check=False).out.split()
        protected = {split_tag(i) for i in keep} | {"legacy"}
        old = [t for t in tags if t not in protected and t != "<none>"]
        for tag in old[max(cfg.keep_images - len(protected), 0):]:
            self.sh(f"docker rmi {shlex.quote(cfg.image)}:{shlex.quote(tag)}", check=False)
        self.sh(
            f"ls -1t {self.path('backups')}/*.dump 2>/dev/null | tail -n +{cfg.keep_backups + 1} | xargs -r rm -f",
            check=False,
        )

    # ── one-off containers ─────────────────────────────────────────────────
    def run_once(self, image: str, command: str, env: dict[str, str] | None = None, stream: bool = False):
        flags = "--rm --no-deps -T -l traefik.enable=false"
        for k, v in (env or {}).items():
            flags += f" -e {shlex.quote(f'{k}={v}')}"
        return self.sh(self.compose(f"run {flags} {self.cfg.api_service} {command}", image), stream=stream, timeout=900)

    def precheck(self, image: str) -> None:
        self.run_once(image, "python -c "
                      + shlex.quote("import psycopg2; from app.core.config import settings as s; "
                                    "print('EMAIL_BACKEND =', s.EMAIL_BACKEND)"), stream=True)

    def migrate(self, image: str) -> None:
        self.run_once(image, "alembic upgrade head",
                      env={"PGOPTIONS": f"-c lock_timeout={self.cfg.lock_timeout_ms}"}, stream=True)

    def downgrade(self, image: str, revision: str) -> None:
        self.run_once(image, f"alembic downgrade {shlex.quote(revision)}",
                      env={"PGOPTIONS": f"-c lock_timeout={self.cfg.lock_timeout_ms}"}, stream=True)

    def backup(self) -> str:
        cfg = self.cfg
        folder = self.path("backups")
        self.sh(f"mkdir -p {folder}")
        file = f"{folder}/{cfg.db_name}-{datetime.now():%Y%m%d-%H%M%S}.dump"
        self.sh(f"docker exec {cfg.db_container} pg_dump -U {cfg.db_user} -Fc {cfg.db_name} > {file}")
        size = int(self.sh(f"stat -c %s {file}").out.strip() or 0)
        if size < 1024:
            raise DeployError(f"backup looks empty: {file}")
        self.sh(f"docker exec -i {cfg.db_container} pg_restore -l < {file} > /dev/null")
        return file

    # ── blue/green swap ────────────────────────────────────────────────────
    def service_ids(self, service: str) -> list[str]:
        out = self.sh(
            f"docker ps -aq --filter label=com.docker.compose.project={shlex.quote(self.cfg.project_name)} "
            f"--filter label=com.docker.compose.service={shlex.quote(service)} "
            "--filter label=com.docker.compose.oneoff=False"
        ).out
        return out.split()

    def _state(self, cid: str) -> str:
        return self.sh(f"docker inspect -f '{_HEALTH_FMT}' {cid}", check=False).out.strip() or "missing"

    def wait_healthy(self, cid: str, timeout: int) -> bool:
        waited = 0
        while waited < timeout:
            state = self._state(cid)
            if state == "healthy":
                return True
            if state in ("exited", "dead", "missing"):
                return False
            self._sleep(3)
            waited += 3
        return False

    def wait_stable(self, cid: str, seconds: int) -> bool:
        """For services without a healthcheck (worker): running, and not restarting, for N seconds."""
        waited = 0
        while waited <= seconds:
            if self._state(cid) != "running":
                return False
            self._sleep(2)
            waited += 2
        restarts = self.sh(f"docker inspect -f '{{{{.RestartCount}}}}' {cid}", check=False).out.strip()
        return restarts in ("0", "")

    def swap(self, service: str, image: str, *, has_healthcheck: bool) -> None:
        """Start the new container next to the old one, gate on health, then retire the old one.
        If the new one never gets healthy it is removed and the old one was never touched."""
        cfg = self.cfg
        old = self.service_ids(service)
        self.sh(
            self.compose(f"up -d --no-deps --no-recreate --scale {shlex.quote(service)}={len(old) + 1} "
                         f"{shlex.quote(service)}", image),
            stream=True, timeout=600,
        )
        new = [c for c in self.service_ids(service) if c not in old]
        if len(new) != 1:
            raise DeployError(f"expected one new {service} container, found {len(new)}")
        cid = new[0]
        ok = (self.wait_healthy(cid, cfg.health_timeout) if has_healthcheck
              else self.wait_stable(cid, cfg.worker_stable_seconds))
        if not ok:
            logs = self.sh(f"docker logs --tail 40 {cid} 2>&1", check=False).out
            self.sh(f"docker rm -f {cid}", check=False)
            raise DeployError(
                f"new {service} container did not become healthy; removed it, the previous one is still serving.\n{logs}"
            )
        ui.info(f"{service}: new container {cid[:12]} is healthy")
        self._sleep(cfg.settle_seconds)  # let Traefik see it before the old one drains
        if old:
            ui.info(f"{service}: draining {len(old)} old container(s) (up to {cfg.drain_seconds}s)")
            joined = " ".join(old)
            self.sh(f"docker stop -t {cfg.drain_seconds} {joined}", timeout=cfg.drain_seconds * len(old) + 60)
            self.sh(f"docker rm {joined}", check=False)

    # ── verification ───────────────────────────────────────────────────────
    def verify(self) -> list[str]:
        cfg = self.cfg
        warnings: list[str] = []
        api = self.service_ids(cfg.api_service)
        if not api:
            raise DeployError("no api container is running after the swap")
        self.sh(f"docker exec {api[0]} curl -fsS http://localhost:8000/health")
        if cfg.health_url:
            for attempt in range(5):
                try:
                    with urlopen(cfg.health_url, timeout=10) as resp:
                        if resp.status == 200:
                            ui.ok(f"public health check OK: {cfg.health_url}")
                            break
                except (URLError, OSError):
                    pass
                self._sleep(3)
            else:
                warnings.append(f"{cfg.health_url} failed from this machine (Traefik / DNS?)")
        for cid in self.service_ids(cfg.worker_service):
            if "Traceback" in self.sh(f"docker logs --tail 80 {cid} 2>&1", check=False).out:
                warnings.append(f"worker {cid[:12]} log contains a traceback")
        return warnings
