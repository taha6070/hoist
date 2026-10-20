from __future__ import annotations

import functools
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Iterator, Optional

import typer

from . import __version__, config, pipeline, ui
from .errors import DeployError
from .server import Server
from .ssh import Remote

app = typer.Typer(
    help="Build locally, push to a registry, migrate and swap containers over SSH with near-zero downtime.",
    no_args_is_help=True,
    add_completion=False,
)
env_app = typer.Typer(help="Manage the server's .env.production", no_args_is_help=True)
app.add_typer(env_app, name="env")

ConfigOpt = Annotated[str, typer.Option("--config", "-c", help="Path to deploy.yml")]


@contextmanager
def _session(config_path: str) -> Iterator[tuple[config.Config, Server]]:
    cfg = config.load(config_path)
    with Remote(cfg) as remote:
        yield cfg, Server(cfg, remote)


def _guard(fn):
    """Run a command body; turn DeployError into a clean exit instead of a traceback."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DeployError as exc:
            ui.error(str(exc))
            raise typer.Exit(1)
        except KeyboardInterrupt:
            ui.error("interrupted")
            raise typer.Exit(130)
    return wrapper


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
@_guard
def init(path: Annotated[Path, typer.Argument(help="Where to write deploy.yml")] = Path("deploy.yml")) -> None:
    """Create deploy.yml by answering a few questions."""
    if path.exists() and not typer.confirm(f"{path} exists. Overwrite?"):
        raise DeployError("aborted")
    host = typer.prompt("Server host / IP")
    user = typer.prompt("SSH user", default="deploy")
    registry_username = typer.prompt("Registry username (GitHub username for ghcr.io)")
    image = typer.prompt("Image name", default=f"ghcr.io/{registry_username.lower()}/homealert-api")
    project = typer.prompt("Docker compose project name on the server (docker inspect the db container's "
                           "com.docker.compose.project label)", default="backend")
    remote_dir = typer.prompt("Deploy directory on the server", default="/opt/homealert")
    health = typer.prompt("Public health URL", default="https://api.thehomealert.com/health")
    path.write_text(
        config.TEMPLATE.format(project_name=project, host=host, user=user, remote_dir=remote_dir,
                               registry_username=registry_username, image=image, health_url=health),
        encoding="utf-8",
    )
    ui.ok(f"wrote {path}. Next: hoist setup")


@app.command()
@_guard
def setup(config_path: ConfigOpt = config.DEFAULT_CONFIG) -> None:
    """One-time server preparation: dirs, docker network, registry login, compose file."""
    with _session(config_path) as (cfg, srv):
        pipeline.setup(cfg, srv)


@app.command()
@_guard
def deploy(
    config_path: ConfigOpt = config.DEFAULT_CONFIG,
    branch: Annotated[Optional[str], typer.Option(help="Branch to deploy (default: git.branch in deploy.yml)")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the 'deploy this commit?' prompt")] = False,
    allow_breaking: Annotated[bool, typer.Option(help="Run migrations the safety scan flags as breaking")] = False,
    skip_build: Annotated[bool, typer.Option(help="Reuse an image already in the registry for this tag")] = False,
    force: Annotated[bool, typer.Option(help="Redeploy even if this tag is already running")] = False,
) -> None:
    """Build the deploy branch tip, push, back up, migrate, swap with zero downtime, verify."""
    with _session(config_path) as (cfg, srv):
        pipeline.deploy(cfg, srv, branch=branch, yes=yes, allow_breaking=allow_breaking,
                        skip_build=skip_build, force=force)


@app.command()
@_guard
def rollback(
    config_path: ConfigOpt = config.DEFAULT_CONFIG,
    to: Annotated[Optional[str], typer.Option(help="Image tag to roll back to (default: previous release)")] = None,
    downgrade: Annotated[bool, typer.Option(help="Also run alembic downgrade (can lose data)")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the downgrade confirmation")] = False,
) -> None:
    """Swap back to the previous release. The schema is untouched unless --downgrade."""
    with _session(config_path) as (cfg, srv):
        pipeline.rollback(cfg, srv, to=to, downgrade=downgrade, yes=yes)


@app.command()
@_guard
def restart(config_path: ConfigOpt = config.DEFAULT_CONFIG) -> None:
    """Zero-downtime restart of the running release (picks up a new .env.production)."""
    with _session(config_path) as (cfg, srv):
        pipeline.restart(cfg, srv)


@app.command()
@_guard
def status(config_path: ConfigOpt = config.DEFAULT_CONFIG) -> None:
    """Running release, database revision, containers and recent releases."""
    with _session(config_path) as (cfg, srv):
        pipeline.status(cfg, srv)


@app.command()
@_guard
def logs(
    service: Annotated[str, typer.Argument(help="api or worker")] = "api",
    config_path: ConfigOpt = config.DEFAULT_CONFIG,
    tail: Annotated[int, typer.Option(help="Lines to show")] = 100,
    follow: Annotated[bool, typer.Option("--follow", "-f")] = False,
) -> None:
    """Show container logs."""
    with _session(config_path) as (cfg, srv):
        pipeline.logs(cfg, srv, service=service, tail=tail, follow=follow)


@env_app.command("push")
@_guard
def env_push(
    config_path: ConfigOpt = config.DEFAULT_CONFIG,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Upload the local env file (shows key names that changed, never values)."""
    with _session(config_path) as (cfg, srv):
        pipeline.env_push(cfg, srv, yes=yes)


if __name__ == "__main__":
    app()
