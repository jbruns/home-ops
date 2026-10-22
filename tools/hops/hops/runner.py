"""The Runner: how hops runs docker on the Host.

Every Host call is `ssh … user@host sudo -n docker …`, the least-privilege path the agent account
has (sudoers allows only /usr/bin/docker). Connections are shared through ControlMaster so a
command that makes several calls pays for one handshake. Nothing is installed on the Host.

Tests swap `process` for a fake that records the argv (tests/conftest.py FakeHost).
"""
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from hops.config import HostConfig
from hops.errors import HopsError

SSH_FAILED = 255


@dataclass(frozen=True)
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


class Process(Protocol):
    def __call__(self, argv: list[str], *, input: str | None = None, capture: bool = True) -> Result: ...


def run_process(argv: list[str], *, input: str | None = None, capture: bool = True) -> Result:
    """Run argv locally; with capture=False stdin/stdout/stderr stay attached to the terminal."""
    if capture:
        done = subprocess.run(argv, input=input, capture_output=True, text=True)
        return Result(argv, done.returncode, done.stdout, done.stderr)
    try:
        done = subprocess.run(argv, input=input, text=True)
    except KeyboardInterrupt:  # Ctrl-C ends `logs -f`; that is not an error
        return Result(argv, 130, "", "")
    return Result(argv, done.returncode, "", "")


class Runner:
    def __init__(self, host: HostConfig, process: Process = run_process):
        self.host = host
        self.process = process

    @property
    def destination(self) -> str:
        return f"{self.host.user}@{self.host.host}"

    def ssh_argv(self, remote: str, *, tty: bool = False) -> list[str]:
        return [
            "ssh", "-i", str(self.host.key),
            "-o", "BatchMode=yes",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self.host.control_dir}/%C",
            "-o", "ControlPersist=10m",
            "-t" if tty else "-T",
            self.destination, remote,
        ]

    def docker(self, *args: str, input: str | None = None, check: bool = True) -> Result:
        """Run `docker <args>` on the Host and capture its output."""
        return self.pipeline(args, input=input, check=check)

    def pipeline(self, *commands: Sequence[str], input: str | None = None, check: bool = True) -> Result:
        """Run docker commands piped into each other on the Host, so data between them never
        reaches this machine (e.g. a secret read in one container and fed to another)."""
        remote = " | ".join(shlex.join(["sudo", "-n", "docker", *c]) for c in commands)
        return self._run(remote, commands[-1], input=input, capture=True, check=check)

    def attach(self, *args: str, tty: bool = False) -> int:
        """Run `docker <args>` with this terminal attached (follow logs, interactive exec)."""
        return self._run(shlex.join(["sudo", "-n", "docker", *args]), args,
                         input=None, capture=False, check=False, tty=tty).returncode

    def _run(self, remote: str, last: Sequence[str], *, input, capture, check, tty=False) -> Result:
        self.host.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = self.process(self.ssh_argv(remote, tty=tty), input=input, capture=capture)
        if result.returncode == SSH_FAILED and capture:
            raise HopsError(f"ssh {self.destination} failed: {result.stderr.strip()}")
        if check and result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            raise HopsError(f"docker {shlex.join(last)} failed: {detail}")
        return result
