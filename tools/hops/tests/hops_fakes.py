"""Test helpers for hops: a fixture tn/, canned `docker inspect` output, and a fake Host.

Imported by the tests as `hops_fakes`; a module named conftest would clash with other tools' when
pytest collects tools/ together.
"""
import shlex
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip("\n"))


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


# A small tn/ with includes, a Stack whose name is also one of its Services (nextcloud), a Service
# name used in two Stacks (exporter), and a Service without container_name (doco-cd).
STACKS = {
    "tn/_baseline.yaml": """
        services:
          baseline:
            restart: unless-stopped
    """,
    "tn/edge/compose.yaml": """
        services:
          traefik:
            extends: {file: ../_baseline.yaml, service: baseline}
            image: traefik:v3.7.15@sha256:aaaa
            container_name: traefik
            labels:
              tier: critical
    """,
    "tn/nextcloud/compose.yaml": """
        include:
          - ./mariadb/service.yaml
          - ./nextcloud/service.yaml
    """,
    "tn/nextcloud/mariadb/service.yaml": """
        services:
          mariadb:
            image: mariadb:11.8.9@sha256:bbbb
            container_name: mariadb
            labels: [tier=critical, startup-order=1]
          exporter:
            image: prom/mysqld-exporter:v0.20.0@sha256:cccc
            container_name: nextcloud-exporter
            labels: {tier: info}
    """,
    "tn/nextcloud/nextcloud/service.yaml": """
        services:
          nextcloud:
            image: nextcloud:35.0.2@sha256:dddd
            container_name: nextcloud
            labels: {tier: critical}
          valkey:
            image: valkey/valkey:9.1.2-alpine@sha256:eeee
            container_name: valkey
            labels: {tier: critical}
    """,
    "tn/sonarr/compose.yaml": """
        services:
          sonarr:
            image: ghcr.io/home-operations/sonarr:4.0.20.3012@sha256:ffff
            container_name: sonarr
            labels: {tier: warning}
          exporter:
            image: ghcr.io/onedr0p/exportarr:v2.3.0
            container_name: sonarr-exporter
            labels: {tier: info}
    """,
    "tn/monitoring/compose.yaml": """
        services:
          prometheus:
            image: prom/prometheus:v3.15.0@sha256:2222
            container_name: prometheus
            labels: {tier: critical}
          alertmanager:
            image: prom/alertmanager:v0.34.1@sha256:3333
            container_name: alertmanager
            labels: {tier: critical}
    """,
    "tn/doco-cd/compose.yaml": """
        services:
          doco-cd:
            image: ghcr.io/kimdre/doco-cd:0.128.0@sha256:1111
            labels: {tier: critical}
    """,
}


@dataclass
class Call:
    argv: list[str]
    input: str | None
    capture: bool

    @property
    def remote(self) -> str:
        """The command line ssh runs on the Host (its last argument)."""
        return self.argv[-1]

    @property
    def docker(self) -> list[list[str]]:
        """The docker arguments of each command in the remote pipeline, without `sudo -n docker`."""
        segments, current = [], []
        for token in shlex.split(self.remote) + ["|"]:
            if token == "|":
                assert current[:3] == ["sudo", "-n", "docker"], current
                segments.append(current[3:])
                current = []
            else:
                current.append(token)
        return segments


class FakeHost:
    """Stands in for the ssh process: records every argv and answers with canned output.

    `respond(*docker_args, stdout=...)` answers any call whose last pipeline command starts with
    those docker arguments (and ends with `suffix`, if given); the latest matching response wins. Unanswered calls fail the test, so
    a test that expects no Host access needs no extra assertion beyond `calls == []`.
    """

    def __init__(self):
        self.calls: list[Call] = []
        self._responses: list[tuple[tuple[str, ...], tuple[str, ...], int, str, str]] = []

    def respond(self, *docker_args: str, suffix: tuple[str, ...] = (), stdout: str = "", stderr: str = "",
                returncode: int = 0):
        self._responses.append((docker_args, tuple(suffix), returncode, stdout, stderr))

    def __call__(self, argv, *, input=None, capture=True):
        from hops.runner import Result

        call = Call(list(argv), input, capture)
        self.calls.append(call)
        last = tuple(call.docker[-1])
        for prefix, suffix, returncode, stdout, stderr in reversed(self._responses):
            if last[:len(prefix)] == prefix and last[len(last) - len(suffix):] == suffix:
                return Result(list(argv), returncode, stdout if capture else "", stderr if capture else "")
        raise AssertionError(f"unexpected Host call: {call.remote}")

    @property
    def docker_calls(self) -> list[list[str]]:
        """The docker arguments of the last command of each call, in order."""
        return [c.docker[-1] for c in self.calls]


def container(name: str, *, project: str | None = None, service: str | None = None, image: str = "busybox:1",
              status: str = "running", health: str | None = "healthy", restarts: int = 0, exit_code: int = 0,
              oom_killed: bool = False, memory: int = 0, health_log: list | None = None,
              started_at: str = "2026-10-21T20:00:00.000000000Z") -> dict:
    """One element of `docker inspect` output, trimmed to the fields hops reads."""
    labels = {}
    if project:
        labels["com.docker.compose.project"] = project
    if service:
        labels["com.docker.compose.service"] = service
    state = {"Status": status, "Running": status == "running", "OOMKilled": oom_killed, "ExitCode": exit_code,
             "StartedAt": started_at, "FinishedAt": "0001-01-01T00:00:00Z"}
    if health:
        state["Health"] = {"Status": health, "FailingStreak": 0, "Log": health_log or []}
    return {
        "Id": f"{name}-id".ljust(64, "0"),
        "Name": f"/{name}",
        "Created": "2026-10-21T19:00:00.000000000Z",
        "Image": "sha256:" + "9" * 64,
        "RestartCount": restarts,
        "State": state,
        "Config": {"Image": image, "Labels": labels},
        "HostConfig": {"Memory": memory},
    }
