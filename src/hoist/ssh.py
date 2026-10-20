"""Thin Paramiko wrapper. Pure Python, so it works on Windows without ssh.exe or WSL."""

from __future__ import annotations

import getpass
import os
import sys
import time
from dataclasses import dataclass
from typing import Protocol

import paramiko

from .config import Config
from .errors import DeployError


@dataclass
class Result:
    code: int
    out: str
    err: str


class Runner(Protocol):
    """What the rest of the tool needs from a connection (also what tests fake)."""

    def run(self, cmd: str, *, check: bool = True, stream: bool = False,
            stdin: str | None = None, timeout: float | None = None) -> Result: ...

    def put_text(self, path: str, content: str, mode: int = 0o644) -> None: ...


class Remote:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        self.client.set_missing_host_key_policy(
            paramiko.AutoAddPolicy() if cfg.accept_new_host_key else paramiko.RejectPolicy()
        )

    def connect(self) -> "Remote":
        cfg = self.cfg
        key = os.path.expanduser(cfg.key_file) if cfg.key_file else None
        passphrase = os.environ.get("HOIST_SSH_PASSPHRASE")
        for _ in range(2):
            try:
                self.client.connect(
                    cfg.host, port=cfg.port, username=cfg.user, key_filename=key,
                    passphrase=passphrase, allow_agent=True, look_for_keys=key is None,
                    timeout=15, banner_timeout=15,
                )
                self.client.get_transport().set_keepalive(30)
                return self
            except paramiko.PasswordRequiredException:
                if passphrase is not None or not sys.stdin.isatty():
                    raise DeployError("SSH key is encrypted: set HOIST_SSH_PASSPHRASE or load it into ssh-agent")
                passphrase = getpass.getpass(f"Passphrase for {key}: ")
            except paramiko.BadHostKeyException as exc:
                raise DeployError(f"Host key for {cfg.host} CHANGED ({exc}). Possible MITM; fix known_hosts.") from exc
            except paramiko.AuthenticationException as exc:
                raise DeployError(f"SSH auth failed for {cfg.user}@{cfg.host}: {exc}") from exc
            except paramiko.SSHException as exc:
                if "known_hosts" in str(exc):
                    raise DeployError(
                        f"{cfg.host} is not in known_hosts. Verify the fingerprint with "
                        f"`ssh {cfg.user}@{cfg.host}` once, or set server.accept_new_host_key: true."
                    ) from exc
                raise DeployError(f"SSH error: {exc}") from exc
            except OSError as exc:
                raise DeployError(f"Cannot reach {cfg.host}:{cfg.port}: {exc}") from exc
        raise DeployError("SSH connection failed")

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Remote":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def run(self, cmd: str, *, check: bool = True, stream: bool = False,
            stdin: str | None = None, timeout: float | None = None) -> Result:
        chan = self.client.get_transport().open_session()
        chan.exec_command(cmd)
        if stdin is not None:
            chan.sendall(stdin.encode())
            chan.shutdown_write()
        out: list[bytes] = []
        err: list[bytes] = []
        deadline = time.monotonic() + timeout if timeout else None
        try:
            while True:
                moved = False
                if chan.recv_ready():
                    data = chan.recv(65536)
                    out.append(data)
                    moved = True
                    if stream:
                        sys.stdout.write(data.decode(errors="replace"))
                        sys.stdout.flush()
                if chan.recv_stderr_ready():
                    data = chan.recv_stderr(65536)
                    err.append(data)
                    moved = True
                    if stream:
                        sys.stderr.write(data.decode(errors="replace"))
                        sys.stderr.flush()
                if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                    break
                if deadline and time.monotonic() > deadline:
                    chan.close()
                    raise DeployError(f"remote command timed out after {timeout}s: {cmd[:120]}")
                if not moved:
                    time.sleep(0.05)
            code = chan.recv_exit_status()
        except KeyboardInterrupt:
            chan.close()
            raise
        result = Result(code, b"".join(out).decode(errors="replace"), b"".join(err).decode(errors="replace"))
        if check and code != 0:
            detail = (result.err or result.out).strip()[-1500:]
            raise DeployError(f"remote command failed ({code}): {cmd[:200]}\n{detail}")
        return result

    def put_text(self, path: str, content: str, mode: int = 0o644) -> None:
        sftp = self.client.open_sftp()
        try:
            tmp = f"{path}.tmp"
            with sftp.open(tmp, "w") as fh:
                fh.write(content)
            sftp.chmod(tmp, mode)
            sftp.posix_rename(tmp, path)
        finally:
            sftp.close()
