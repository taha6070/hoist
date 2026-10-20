"""The commands, as sequences of steps. Order matters: every step that can fail runs before the
first step that changes what users see."""

from __future__ import annotations

import os
import shlex
import sys
from getpass import getpass

from . import local, migrations, ui
from .config import Config
from .errors import DeployError
from .server import COMPOSE_NAME, ENV_NAME, RELEASE_ENV, Server, split_tag

REQUIRED_ENV = ("SECRET_KEY", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "EMAIL_BACKEND", "EMAIL_FROM")


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip("\"'")
    return values


def check_env(env: dict[str, str]) -> None:
    """Same guard the old deploy.sh had: a bad env would crash the new API on startup."""
    for key in REQUIRED_ENV:
        if not env.get(key):
            raise DeployError(f"{key} is empty in {ENV_NAME} on the server")
    backend = env["EMAIL_BACKEND"]
    if backend in ("mailhog", "null"):
        raise DeployError(f"EMAIL_BACKEND={backend} is not a production backend")
    if backend == "resend" and not env.get("RESEND_API_KEY"):
        raise DeployError("EMAIL_BACKEND=resend but RESEND_API_KEY is empty: the API would crash on startup")


def _report_findings(findings: list[migrations.Finding], allow_breaking: bool) -> None:
    for f in findings:
        label = "BLOCKING" if f.blocking else "note"
        ui.warn(f"[{label}] {f.revision} line {f.line}: {f.message}")
    blocking = [f for f in findings if f.blocking]
    if blocking and not allow_breaking:
        raise DeployError(
            "Pending migrations are not safe to run while the previous release is serving "
            "(expand/contract: ship additive changes first, remove old things in a later release).\n"
            "If you accept a short outage or know it is safe, re-run with --allow-breaking."
        )
    if blocking:
        ui.warn("--allow-breaking given: continuing despite the findings above")


def _swap_all(srv: Server, image: str) -> None:
    srv.swap(srv.cfg.api_service, image, has_healthcheck=True)
    srv.swap(srv.cfg.worker_service, image, has_healthcheck=False)


def deploy(cfg: Config, srv: Server, *, branch: str | None, yes: bool, allow_breaking: bool,
           skip_build: bool, force: bool) -> None:
    ui.step("Preflight (local)")
    if not skip_build:
        local.docker_ready()
    release = local.resolve_release(cfg, branch)
    ui.info(f"branch : {release.branch} (in sync with {cfg.git_remote})")
    ui.info(f"commit : {release.sha}  {release.subject}  ({release.author})")
    ui.info("Only this commit is deployed; uncommitted files in your working tree are ignored.")
    if not yes and not _confirm("Deploy this commit? [y/N] ", "y"):
        raise DeployError("aborted (use --yes to skip the prompt)")
    with local.export(cfg, release) as src:
        _deploy(cfg, srv, release, src, allow_breaking=allow_breaking, skip_build=skip_build, force=force)


def _deploy(cfg: Config, srv: Server, release: local.Release, src: local.Source, *,
            allow_breaking: bool, skip_build: bool, force: bool) -> None:
    tag = release.tag
    image = f"{cfg.image}:{tag}"
    ui.info(f"image: {image}")

    ui.step("Preflight (server)")
    srv.preflight()
    check_env(parse_env(srv.sh(f"cat {srv.path(ENV_NAME)}").out))
    current = srv.current_image()
    if current == image and not force:
        ui.ok(f"{image} is already the running release. Nothing to do (use --force to redeploy).")
        return
    current_rev = srv.db_revision()
    revs = migrations.load_revisions(src.migrations_dir)
    todo = migrations.pending(revs, current_rev)
    head = migrations.head_of(revs)
    ui.info(f"database revision: {current_rev or 'none'} -> {head} ({len(todo)} pending)")
    _report_findings(migrations.scan(todo), allow_breaking)

    if not skip_build:
        ui.step(f"Building {image} locally")
        local.build(cfg, image, src.context)
        ui.step("Pushing to the registry")
        local.push(image)

    ui.step("Pulling on the server (current containers keep serving)")
    srv.r.put_text(srv.path(COMPOSE_NAME), src.compose_file.read_text(encoding="utf-8"))
    srv.pull(image)

    prev_image = current
    if prev_image is None:
        legacy = srv.adopt_legacy(srv.service_ids(cfg.api_service))
        if legacy:
            ui.info(f"first deploy over a server-built stack: current image kept as {legacy} for rollback")
            srv.record(legacy, kind="legacy", rev=current_rev)
            prev_image = legacy

    ui.step("Checking the new image can start (no traffic)")
    srv.precheck(image)

    ui.step("Backing up the database")
    backup = srv.backup()
    ui.ok(f"backup verified: {backup}")

    if todo:
        ui.step(f"Migrating ({len(todo)} revision(s), lock_timeout={cfg.lock_timeout_ms}ms); old release still serving")
        srv.migrate(image)
    else:
        ui.info("no pending migrations")

    ui.step("Swapping containers (new starts beside old; old drains only after new is healthy)")
    _swap_all(srv, image)

    ui.step("Verifying")
    warnings = srv.verify()
    for w in warnings:
        ui.warn(w)

    srv.record(image, kind="deploy", prev_image=prev_image, rev_before=current_rev, rev_after=head,
               backup=backup, git=tag)
    srv.prune([image, prev_image or image])
    ui.step("Done")
    ui.ok(f"{image} is live. Roll back with: hoist rollback")


def rollback(cfg: Config, srv: Server, *, to: str | None, downgrade: bool, yes: bool) -> None:
    ui.step("Preflight")
    srv.preflight()
    current = srv.current_image()
    rows = srv.releases()
    cur_row = next((r for r in reversed(rows) if r.get("image") == current), None)
    if to:
        target = f"{cfg.image}:{to}"
    elif cur_row and cur_row.get("prev_image"):
        target = cur_row["prev_image"]
    else:
        raise DeployError("no previous release recorded; pass --to <tag>")
    if target == current:
        raise DeployError(f"{target} is already running")
    ui.info(f"{current} -> {target}")

    if not srv.has_image(target):
        ui.step("Pulling the target image")
        srv.pull(target)

    if downgrade:
        rev = (cur_row or {}).get("rev_before")
        if not rev:
            raise DeployError("this release did not record a previous schema revision; cannot downgrade")
        ui.warn(f"--downgrade will run `alembic downgrade {rev}` and may DELETE data written since the deploy.")
        if not yes and not _confirm("Type 'downgrade' to continue: ", "downgrade"):
            raise DeployError("aborted")
        ui.step("Backing up before the downgrade")
        ui.ok(f"backup verified: {srv.backup()}")
        ui.step(f"Downgrading schema to {rev} (using the newer image, which has those revisions)")
        srv.downgrade(current, rev)

    ui.step("Swapping back")
    _swap_all(srv, target)
    for w in srv.verify():
        ui.warn(w)
    srv.record(target, kind="rollback", prev_image=current, rev_before=srv.db_revision())
    ui.ok(f"rolled back to {target}")
    if not downgrade:
        ui.info("The database schema was left as-is (additive migrations are compatible with the old code).")


def restart(cfg: Config, srv: Server) -> None:
    """Blue/green restart of the running release, e.g. after `env push`."""
    srv.preflight()
    current = srv.current_image()
    if not current:
        raise DeployError("nothing deployed yet")
    ui.step(f"Restarting {current} with zero downtime")
    _swap_all(srv, current)
    for w in srv.verify():
        ui.warn(w)
    ui.ok("restarted")


def setup(cfg: Config, srv: Server) -> None:
    ui.step("Preparing the server")
    srv.sh("docker version --format '{{.Server.Version}}'")
    srv.sh("docker compose version --short")
    srv.sh(f"mkdir -p {shlex.quote(srv.dir)}/backups && touch {srv.path(RELEASE_ENV)}")
    srv.sh("docker network inspect traefik >/dev/null 2>&1 || docker network create traefik")
    srv.r.put_text(srv.path(COMPOSE_NAME), cfg.compose_file.read_text(encoding="utf-8"))
    ui.ok(f"{srv.dir} ready, compose file uploaded")

    token = os.environ.get("HOIST_REGISTRY_TOKEN") or (
        getpass(f"Registry token for {cfg.registry_username}@{cfg.registry_server} (read-only; empty = skip): ")
        if sys.stdin.isatty() else ""
    )
    if token:
        srv.sh(
            f"docker login {shlex.quote(cfg.registry_server)} -u {shlex.quote(cfg.registry_username)} --password-stdin",
            stdin=token,
        )
        ui.ok("server logged in to the registry")
    else:
        ui.warn("skipped registry login; the server cannot pull private images until you run setup again")

    if srv._exists(ENV_NAME):
        ui.ok(f"{ENV_NAME} present on the server")
    else:
        ui.warn(f"{ENV_NAME} not on the server yet: run `hoist env push`")
    label = srv.sh(
        f"docker inspect -f '{{{{index .Config.Labels \"com.docker.compose.project\"}}}}' {cfg.db_container} 2>/dev/null || true"
    ).out.strip()
    if label and label != cfg.project_name:
        ui.warn(f"running stack belongs to compose project '{label}', deploy.yml says '{cfg.project_name}'. Fix it.")
    elif label:
        ui.ok(f"compose project '{label}' matches deploy.yml")


def env_push(cfg: Config, srv: Server, *, yes: bool) -> None:
    if not cfg.env_file.is_file():
        raise DeployError(f"{cfg.env_file} not found")
    new_text = cfg.env_file.read_text(encoding="utf-8")
    new = parse_env(new_text)
    check_env(new)
    old_text = srv.sh(f"cat {srv.path(ENV_NAME)} 2>/dev/null || true").out
    old = parse_env(old_text)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(new) & set(old) if new[k] != old[k])
    ui.info(f"added: {', '.join(added) or '-'}\nremoved: {', '.join(removed) or '-'}\nchanged: {', '.join(changed) or '-'}")
    if not (added or removed or changed):
        ui.ok("server env already matches")
        return
    if not yes and not _confirm("Push this env to the server? [y/N] ", "y"):
        raise DeployError("aborted")
    if old_text:
        srv.sh(f"cp {srv.path(ENV_NAME)} {srv.path(ENV_NAME)}.bak")
    srv.r.put_text(srv.path(ENV_NAME), new_text, mode=0o600)
    ui.ok("env pushed (previous copy kept as .env.production.bak). Apply it with: hoist restart")


def status(cfg: Config, srv: Server) -> None:
    current = srv.current_image()
    ui.info(f"running release : {current or 'none recorded'}")
    ui.info(f"database rev    : {srv.db_revision() or 'none'}")
    try:
        head = migrations.head_of(migrations.load_revisions(cfg.migrations_dir))
        ui.info(f"local head      : {head}")
    except DeployError as exc:
        ui.warn(str(exc))
    ui.info("\ncontainers:")
    ui.info(srv.sh(
        f"docker ps -a --filter label=com.docker.compose.project={shlex.quote(cfg.project_name)} "
        "--format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'"
    ).out)
    rows = srv.releases()[-8:]
    ui.info("recent releases:")
    for r in reversed(rows):
        ui.info(f"  {r.get('ts', '?')}  {r.get('kind', '?'):8}  {r.get('tag', '?')}  rev {r.get('rev_after') or r.get('rev') or '-'}")


def logs(cfg: Config, srv: Server, *, service: str, tail: int, follow: bool) -> None:
    svc = {"api": cfg.api_service, "worker": cfg.worker_service}.get(service, service)
    ids = srv.service_ids(svc)
    if not ids:
        raise DeployError(f"no container for service '{svc}'")
    flag = "-f " if follow else ""
    try:
        srv.sh(f"docker logs {flag}--tail {int(tail)} {ids[-1]} 2>&1", stream=True)
    except KeyboardInterrupt:
        pass


def _confirm(prompt: str, expected: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(prompt).strip().lower() == expected.lower()
