from __future__ import annotations

import json
import shlex

import pytest

from conftest import FakeRemote
from hoist import pipeline
from hoist.errors import DeployError
from hoist.server import Server, split_tag
from hoist.ssh import Result

NEW = "ghcr.io/me/homealert-api:abc123"


def make(cfg, rules):
    remote = FakeRemote(rules)
    return Server(cfg, remote, sleep=lambda _s: None), remote


def test_split_tag() -> None:
    assert split_tag("ghcr.io/me/x:abc") == "abc"
    assert split_tag("localhost:5000/x") == ""


def test_compose_uses_project_both_env_files_and_inline_image(cfg) -> None:
    srv, _ = make(cfg, [])
    cmd = srv.compose("ps", NEW)
    assert "-p backend" in cmd and "--env-file .env.production --env-file .release.env" in cmd
    assert cmd.index(f"API_IMAGE={NEW}") < cmd.index("docker compose")


def test_swap_starts_new_beside_old_then_drains_old(cfg) -> None:
    srv, remote = make(cfg, [
        ("docker ps -aq", [Result(0, "old1\n", ""), Result(0, "old1\nnew1\n", "")]),
        ("docker inspect -f '{{if .State.Health}}", Result(0, "healthy\n", "")),
    ])
    srv.swap("api", NEW, has_healthcheck=True)
    up = next(c for c in remote.calls if " up -d " in c)
    assert "--no-recreate" in up and "--scale api=2" in up and f"API_IMAGE={NEW}" in up
    stop = next(i for i, c in enumerate(remote.calls) if "docker stop" in c)
    healthy = next(i for i, c in enumerate(remote.calls) if "docker inspect -f '{{if .State.Health}}" in c)
    assert healthy < stop and "old1" in remote.calls[stop] and "new1" not in remote.calls[stop]


def test_unhealthy_new_container_is_removed_and_old_is_never_stopped(cfg) -> None:
    srv, remote = make(cfg, [
        ("docker ps -aq", [Result(0, "old1\n", ""), Result(0, "old1\nnew1\n", "")]),
        ("docker inspect -f '{{if .State.Health}}", Result(0, "exited\n", "")),
        ("docker logs", Result(0, "boom", "")),
    ])
    with pytest.raises(DeployError, match="previous one is still serving"):
        srv.swap("api", NEW, has_healthcheck=True)
    assert remote.ran("docker rm -f new1")
    assert not remote.ran("docker stop")


def test_first_ever_swap_with_nothing_running(cfg) -> None:
    srv, remote = make(cfg, [
        ("docker ps -aq", [Result(0, "", ""), Result(0, "new1\n", "")]),
        ("docker inspect -f '{{if .State.Health}}", Result(0, "healthy\n", "")),
    ])
    srv.swap("api", NEW, has_healthcheck=True)
    assert "--scale api=1" in next(c for c in remote.calls if " up -d " in c)
    assert not remote.ran("docker stop")


def test_worker_restarting_counts_as_unstable(cfg) -> None:
    srv, remote = make(cfg, [
        ("docker ps -aq", [Result(0, "old1\n", ""), Result(0, "old1\nnew1\n", "")]),
        ("docker inspect -f '{{if .State.Health}}", Result(0, "running\n", "")),
        ("RestartCount", Result(0, "2\n", "")),
    ])
    with pytest.raises(DeployError):
        srv.swap("worker", NEW, has_healthcheck=False)
    assert not remote.ran("docker stop")


def test_migrate_runs_new_image_out_of_traefik_with_lock_timeout(cfg) -> None:
    srv, remote = make(cfg, [])
    srv.migrate(NEW)
    cmd = remote.calls[-1]
    assert "run --rm --no-deps -T -l traefik.enable=false" in cmd
    assert "PGOPTIONS=-c lock_timeout=5000" in cmd
    assert f"API_IMAGE={NEW}" in cmd and cmd.rstrip().endswith("alembic upgrade head")


def test_db_revision_fresh_database_is_none(cfg) -> None:
    srv, _ = make(cfg, [("psql", Result(1, "", 'relation "alembic_version" does not exist'))])
    assert srv.db_revision() is None


def test_db_revision_error_is_not_swallowed(cfg) -> None:
    srv, _ = make(cfg, [("psql", Result(1, "", "connection refused"))])
    with pytest.raises(DeployError, match="cannot read"):
        srv.db_revision()


def test_backup_rejects_an_empty_dump(cfg) -> None:
    srv, _ = make(cfg, [("stat -c %s", Result(0, "12\n", ""))])
    with pytest.raises(DeployError, match="empty"):
        srv.backup()


def test_wrong_compose_project_is_refused_before_touching_anything(cfg) -> None:
    srv, remote = make(cfg, [
        ("test -f", Result(0, "", "")),
        ("grep -x", Result(0, "homealert_db\n", "")),
        ("com.docker.compose.project", Result(0, "other\n", "")),
    ])
    with pytest.raises(DeployError, match="empty volumes"):
        srv.preflight()
    assert not remote.ran(" up ")


def test_record_writes_log_line_and_release_env(cfg) -> None:
    srv, remote = make(cfg, [])
    row = srv.record(NEW, kind="deploy", prev_image="x:1")
    assert row["tag"] == "abc123"
    log_cmd = next(c for c in remote.calls if "releases.jsonl" in c)
    payload = shlex.split(log_cmd)[2]  # printf '%s\n' <json> >> file
    assert json.loads(payload)["prev_image"] == "x:1"
    assert any(".release.env" in c and NEW in c for c in remote.calls)


def test_parse_and_check_env() -> None:
    env = pipeline.parse_env('# c\nSECRET_KEY="s"\nPOSTGRES_PASSWORD=p\nREDIS_PASSWORD=r\n'
                             "EMAIL_BACKEND=resend\nEMAIL_FROM=a@b.c\nRESEND_API_KEY=k\n")
    pipeline.check_env(env)
    env["RESEND_API_KEY"] = ""
    with pytest.raises(DeployError, match="RESEND_API_KEY"):
        pipeline.check_env(env)
    env["EMAIL_BACKEND"] = "mailhog"
    with pytest.raises(DeployError, match="not a production backend"):
        pipeline.check_env(env)
