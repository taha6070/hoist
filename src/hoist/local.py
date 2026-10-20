"""Local machine work: git state, docker build and push."""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import Config
from .errors import DeployError


def _run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise DeployError(f"'{args[0]}' not found on PATH") from exc


def _stream(args: list[str], cwd: Path | None = None) -> None:
    try:
        code = subprocess.run(args, cwd=cwd).returncode
    except FileNotFoundError as exc:
        raise DeployError(f"'{args[0]}' not found on PATH") from exc
    if code != 0:
        raise DeployError(f"`{' '.join(args[:3])} ...` failed with exit code {code}")


def docker_ready() -> None:
    proc = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if proc.returncode != 0:
        raise DeployError("Docker daemon is not reachable. Start Docker Desktop and retry.")


@dataclass(frozen=True)
class Release:
    """The exact commit that will be deployed: the tip of the deploy branch, never the working tree."""

    branch: str
    sha: str
    subject: str
    author: str
    toplevel: Path
    prefix: str  # build context relative to the repo root, e.g. "backend/" ("" if the context is the root)

    @property
    def tag(self) -> str:
        return self.sha


@dataclass(frozen=True)
class Source:
    """Where the deployed files live: an export of the release commit."""

    context: Path
    compose_file: Path
    migrations_dir: Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd)


def resolve_release(cfg: Config, branch: str | None = None) -> Release:
    """Pick the commit to deploy. Hard errors unless the local branch exists and equals the remote's."""
    branch = branch or cfg.git_branch
    remote = cfg.git_remote
    cwd = cfg.build_context

    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise DeployError("Not a git repository: hoist only deploys committed code.")
    toplevel = Path(top.stdout.strip())
    prefix = _git(cwd, "rev-parse", "--show-prefix").stdout.strip()

    if _git(toplevel, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
        raise DeployError(
            f"Branch '{branch}' does not exist. Create it from the code you want live: "
            f"git branch {branch} main && git push -u {remote} {branch}"
        )
    local_sha = _git(toplevel, "rev-parse", f"refs/heads/{branch}").stdout.strip()

    fetch = _git(toplevel, "fetch", "--quiet", remote, branch)
    if fetch.returncode != 0:
        detail = fetch.stderr.strip()
        if "couldn't find remote ref" in detail:
            raise DeployError(
                f"Branch '{branch}' is not on '{remote}'. Push it first: git push -u {remote} {branch}"
            )
        raise DeployError(f"git fetch {remote} {branch} failed: {detail}")
    remote_sha = _git(toplevel, "rev-parse", "FETCH_HEAD").stdout.strip()

    if local_sha != remote_sha:
        def is_ancestor(a: str, b: str) -> bool:
            return _git(toplevel, "merge-base", "--is-ancestor", a, b).returncode == 0

        if is_ancestor(remote_sha, local_sha):
            n = _git(toplevel, "rev-list", "--count", f"{remote_sha}..{local_sha}").stdout.strip()
            why = f"is {n} commit(s) ahead of {remote}/{branch}: push first (git push {remote} {branch})"
        elif is_ancestor(local_sha, remote_sha):
            why = f"is behind {remote}/{branch}: update it (git switch {branch} && git pull --ff-only)"
        else:
            why = f"has diverged from {remote}/{branch}: reconcile it before deploying"
        raise DeployError(f"Local branch '{branch}' {why}.")

    short = _git(toplevel, "rev-parse", "--short=10", local_sha).stdout.strip()
    subject, _, author = _git(toplevel, "log", "-1", "--format=%s%x00%an", local_sha).stdout.strip().partition("\x00")
    return Release(branch, short, subject, author, toplevel, prefix)


@contextmanager
def export(cfg: Config, release: Release) -> Iterator[Source]:
    """Check the release commit out into a temp dir (git archive), so nothing from the working tree can leak in."""
    with tempfile.TemporaryDirectory(prefix="hoist-", ignore_cleanup_errors=True) as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / "release.tar"
        with archive.open("wb") as out:
            proc = subprocess.run(
                ["git", "archive", "--format=tar", release.sha, "--", release.prefix or "."],
                cwd=release.toplevel, stdout=out, stderr=subprocess.PIPE, check=False,
            )
        if proc.returncode != 0:
            raise DeployError(f"git archive failed: {proc.stderr.decode(errors='replace').strip()}")
        root = tmp_dir / "src"
        with tarfile.open(archive) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(root, filter="data")
            else:  # pragma: no cover - Python < 3.11.4
                tar.extractall(root)
        archive.unlink()

        context = root / release.prefix if release.prefix else root

        def inside(path: Path, what: str) -> Path:
            try:
                found = context / path.relative_to(cfg.build_context)
            except ValueError:
                raise DeployError(f"{what} ({path}) must be inside build.context ({cfg.build_context})") from None
            if not found.exists():
                raise DeployError(f"{what} '{path.relative_to(cfg.build_context)}' is not committed on branch "
                                  f"'{release.branch}'")
            return found

        yield Source(context, inside(cfg.compose_file, "compose_file"), inside(cfg.migrations_dir, "migrations_dir"))


def build(cfg: Config, image_ref: str, context: Path) -> None:
    _stream(
        ["docker", "build", "--platform", cfg.platform, "-f", cfg.dockerfile, "-t", image_ref, "."],
        cwd=context,
    )


def push(image_ref: str) -> None:
    proc = _run(["docker", "push", image_ref])
    if proc.returncode != 0:
        hint = ""
        if "denied" in proc.stderr or "unauthorized" in proc.stderr:
            hint = "\nLog in first: docker login <registry> (GHCR: a token with write:packages)."
        raise DeployError(f"docker push failed:\n{proc.stderr.strip()[-800:]}{hint}")
