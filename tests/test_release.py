"""Branch-based releases: only the pushed tip of the deploy branch is ever built."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoist import config, local
from hoist.errors import DeployError


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, text=True, capture_output=True, check=True,
    )
    return proc.stdout.strip()


def commit(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", rel)
    git(repo, "commit", "-m", f"edit {rel}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "init", "--initial-branch=main", str(work))
    git(work, "remote", "add", "origin", str(remote))
    commit(work, "backend/Dockerfile", "FROM scratch")
    commit(work, "backend/docker-compose.prod.yml", "services: {}")
    commit(work, "backend/alembic/versions/a.py", "revision = 'a'")
    commit(work, "backend/app.txt", "committed")
    git(work, "branch", "production")
    git(work, "push", "-u", "origin", "main", "production")
    return work


def make_cfg(repo: Path) -> config.Config:
    path = repo / "backend" / "deploy.yml"
    path.write_text(config.TEMPLATE.format(
        project_name="backend", host="1.2.3.4", user="deploy", remote_dir="/opt/homealert",
        registry_username="me", image="ghcr.io/me/homealert-api", health_url="https://x/health"), encoding="utf-8")
    return config.load(path)


def test_release_is_the_branch_tip(repo: Path) -> None:
    rel = local.resolve_release(make_cfg(repo))
    assert rel.branch == "production"
    assert rel.prefix == "backend/"
    assert rel.sha == git(repo, "rev-parse", "--short=10", "production")


def test_resolves_from_any_checked_out_branch(repo: Path) -> None:
    git(repo, "switch", "-c", "feature")
    commit(repo, "backend/app.txt", "feature work")
    rel = local.resolve_release(make_cfg(repo))
    assert rel.sha == git(repo, "rev-parse", "--short=10", "production")


def test_export_ignores_uncommitted_and_untracked(repo: Path) -> None:
    cfg = make_cfg(repo)
    (repo / "backend" / "app.txt").write_text("dirty edit", encoding="utf-8")
    (repo / "backend" / "scratch.txt").write_text("untracked", encoding="utf-8")
    with local.export(cfg, local.resolve_release(cfg)) as src:
        assert (src.context / "app.txt").read_text(encoding="utf-8") == "committed"
        assert not (src.context / "scratch.txt").exists()
        assert (src.context / "Dockerfile").is_file()
        assert src.compose_file.is_file()
        assert (src.migrations_dir / "a.py").is_file()
        kept = src.context
    assert not kept.exists()


def test_export_ignores_unmerged_main(repo: Path) -> None:
    cfg = make_cfg(repo)
    commit(repo, "backend/app.txt", "only on main")
    with local.export(cfg, local.resolve_release(cfg)) as src:
        assert (src.context / "app.txt").read_text(encoding="utf-8") == "committed"


def test_missing_branch(repo: Path) -> None:
    git(repo, "branch", "-D", "production")
    with pytest.raises(DeployError, match="does not exist"):
        local.resolve_release(make_cfg(repo))


def test_branch_not_pushed(repo: Path) -> None:
    git(repo, "branch", "staging")
    with pytest.raises(DeployError, match="not on 'origin'"):
        local.resolve_release(make_cfg(repo), branch="staging")


def test_unpushed_commits_are_an_error(repo: Path) -> None:
    git(repo, "switch", "production")
    commit(repo, "backend/app.txt", "not pushed")
    with pytest.raises(DeployError, match="1 commit.*ahead"):
        local.resolve_release(make_cfg(repo))


def test_behind_remote_is_an_error(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", "--branch", "production", str(tmp_path / "remote.git"), str(other))
    commit(other, "backend/app.txt", "someone else pushed")
    git(other, "push", "origin", "production")
    with pytest.raises(DeployError, match="behind"):
        local.resolve_release(make_cfg(repo))


def test_diverged_is_an_error(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", "--branch", "production", str(tmp_path / "remote.git"), str(other))
    commit(other, "backend/app.txt", "remote side")
    git(other, "push", "origin", "production")
    git(repo, "switch", "production")
    commit(repo, "backend/other.txt", "local side")
    with pytest.raises(DeployError, match="diverged"):
        local.resolve_release(make_cfg(repo))


def test_compose_file_must_be_committed(repo: Path) -> None:
    git(repo, "switch", "production")
    git(repo, "rm", "-q", "backend/docker-compose.prod.yml")
    git(repo, "commit", "-m", "drop compose")
    git(repo, "push", "origin", "production")
    cfg = make_cfg(repo)
    with pytest.raises(DeployError, match="not committed on branch"):
        with local.export(cfg, local.resolve_release(cfg)):
            pass
