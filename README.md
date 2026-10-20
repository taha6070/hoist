# hoist

Build on your machine, push to a registry, migrate, and swap containers on the server with
near-zero downtime. Pure Python: runs on Windows with Docker Desktop and your normal SSH key.
No Ruby, no WSL, nothing built on the server.

## Install (Windows, once)

```powershell
uv tool install "./tools/hoist"      # from the repo root; gives you the `hoist` command
```

Needs Docker Desktop running and `docker login ghcr.io` done locally (token with `write:packages`).

## First-time setup

```powershell
cd backend
hoist init          # writes deploy.yml (host, user, registry, compose project name)
hoist setup         # server dirs, docker network, registry login, uploads compose file
hoist env push      # only if the server has no .env.production yet
hoist deploy
```

* `project_name` must equal the compose project of the running stack
  (`docker inspect homealert_db -f '{{index .Config.Labels "com.docker.compose.project"}}'`).
  `deploy` refuses to run if it does not, because a wrong name would mount empty volumes.
* `setup` asks for a **read-only** registry token for the server (or set `HOIST_REGISTRY_TOKEN`).
* Encrypted SSH key: load it into ssh-agent or set `HOIST_SSH_PASSPHRASE`.
* The first deploy over a stack that was built on the server tags the running image
  `...:legacy` so `rollback` can return to it. That first swap is already zero-downtime.

## Daily use

| Command | What it does |
|---|---|
| `hoist deploy` | build, push, back up, migrate, swap, verify |
| `hoist rollback` | swap back to the previous release (schema untouched) |
| `hoist rollback --downgrade` | also `alembic downgrade` to the revision before that release (can lose data, asks to confirm) |
| `hoist restart` | zero-downtime restart of the running release (after `env push`) |
| `hoist env push` | upload `.env.production`; prints key names that changed, never values |
| `hoist status` / `logs api -f` | what is running, recent releases, logs |

`deploy` flags: `--branch <name>`, `--yes`, `--allow-breaking`, `--skip-build` (image already pushed), `--force`.

## Which code gets deployed

Only the tip of the `production` branch (`git.branch` in `deploy.yml`). The tool exports that commit
with `git archive` into a temp dir and builds from there, so uncommitted or untracked files in your
working tree can never reach the image. The compose file and migrations are read from the same commit.
There is no `--allow-dirty`.

`deploy` stops with an error unless local `production` exists and equals `origin/production`
(not pushed, behind, or diverged are all hard errors). The image tag is the commit SHA.

```powershell
git branch production main && git push -u origin production   # once
git switch production; git merge main; git push                # every release
cd backend; hoist deploy
```

## What `deploy` does, in order

Everything that can fail runs before anything users can see changes.

1. Local: Docker up, `production` pushed and in sync with the remote, tag = commit SHA, confirm prompt.
2. Server: containers up, compose project matches, free disk, `.env.production` valid
   (same checks the old `deploy.sh` had: no mailhog/null backend, Resend key present, ...).
3. Migration scan of the revisions the server has not applied (see below).
4. Build and push locally; server pulls. The old containers keep serving.
5. Start the **new image once with no traffic** (imports the app, loads settings, has psycopg2).
6. `pg_dump` backup, verified with `pg_restore -l`.
7. `alembic upgrade head` in a one-off container with `lock_timeout` (old release still serving).
8. Swap api: the new container starts **beside** the old one, Traefik routes to it only after its
   Docker healthcheck passes, then the old one drains (`docker stop -t drain_seconds`).
   If the new one never gets healthy it is removed and the old one was never touched.
9. Swap worker the same way (old gets SIGTERM, so Dramatiq finishes in-flight jobs).
10. Verify (container health, public health URL, worker traceback check), record the release,
    prune old images and backups.

## Migration rules (why the guard exists)

Migrations run **before** the swap, so for a short time the *old* code runs on the *new* schema.
Each release must therefore work with the schema of both the previous and the next release
(expand, then contract):

* Adding tables/columns (nullable or with a default): ship in one release.
* Renaming, dropping, changing a type, adding NOT NULL: split across releases (add + dual-write,
  switch reads, drop later).

`deploy` scans `upgrade()` of the pending revisions and **blocks** on `drop_table`, `drop_column`,
renames, type changes, `nullable=False`, and destructive raw SQL. Index-without-CONCURRENTLY on an
existing table is a note, not a block. Pass `--allow-breaking` when you accept a short outage or
know it is safe. A failed migration aborts before any swap; rollback never downgrades the schema
unless you ask.

## Limits

* Near-zero, not zero: requests longer than `drain_seconds` are cut when the old container stops.
* Keep `db` and `redis` out of the swap; they are never recreated by this tool.
* Not covered: dashboards (deploy those through Vercel), notifications, multi-server.
