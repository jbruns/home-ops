import subprocess
import io
import json
import sys
import textwrap
from email.message import Message
from dataclasses import dataclass, field
from urllib.error import HTTPError
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import validate as validate_module  # noqa: E402

CIDRS = "173.245.48.0/20,2400:cb00::/32"
SOPS_ENV = "sops_version=3.9.0\n"

# A minimal repository that passes `secrets`, `env`, `cloudflare` and `isolation`.
BASE_FILES = {
    ".doco-cd.yml": "auto_discovery:\n  depth: 1\n",
    "tn/app/compose.yaml": "include:\n  - web/service.yaml\n",
    "tn/app/web/service.yaml": """
        services:
          web:
            image: nginx:1@sha256:abc
            env_file: secret.sops.env
            environment:
              - GREETING=${GREETING}
    """,
    "tn/app/web/secret.sops.env": "TOKEN=ENC[x]\n" + SOPS_ENV,
    "tn/app/.sops.env": "GREETING=ENC[x]\n" + SOPS_ENV,
    "tn/app/env.template": "GREETING=\n",
    "tn/monitoring/compose.yaml": """
        include:
          - prometheus/service.yaml
          - alertmanager/service.yaml
          - loki/service.yaml
          - alloy/service.yaml
          - grafana/service.yaml
    """,
    "tn/monitoring/.sops.env": SOPS_ENV,
    "tn/monitoring/env.template": "",
    "tn/monitoring/prometheus/service.yaml": """
        services:
          prometheus:
            image: prom/prometheus:v1
          dependencies-render:
            image: python:3
            environment:
              - DEP_HOST=example
    """,
    "tn/monitoring/prometheus/dependencies.yml": "- targets: ['${DEP_HOST}']\n",
    "tn/monitoring/prometheus/prometheus.yml": "global: {}\n",
    "tn/monitoring/prometheus/alerting-rules.yml": "groups: []\n",
    "tn/monitoring/alertmanager/service.yaml": "services:\n  alertmanager:\n    image: prom/alertmanager:v2\n",
    "tn/monitoring/alertmanager/config.yml": "route: {}\n",
    "tn/monitoring/loki/service.yaml": "services:\n  loki:\n    image: grafana/loki:3\n",
    "tn/monitoring/loki/loki.yaml": "{}\n",
    "tn/monitoring/alloy/service.yaml": "services:\n  alloy:\n    image: grafana/alloy:1\n",
    "tn/monitoring/alloy/config.alloy": "\n",
    "tn/monitoring/grafana/service.yaml": "services:\n  grafana:\n    image: grafana/grafana:12\n",
    "tn/monitoring/grafana/datasources.sops.env": "GRAFANA_DS_PROMETHEUS_URL=ENC[x]\nGRAFANA_DS_LOKI_URL=ENC[x]\n" + SOPS_ENV,
    "tn/monitoring/grafana/provisioning/datasources.yml": """
        apiVersion: 1
        datasources:
          - {name: Prometheus, uid: prometheus, type: prometheus, isDefault: true, url: $GRAFANA_DS_PROMETHEUS_URL}
          - {name: Loki, uid: loki, type: loki, url: $GRAFANA_DS_LOKI_URL}
    """,
    "tn/monitoring/grafana/provisioning/dashboards.yml": "apiVersion: 1\nproviders: []\n",
    "tn/monitoring/grafana/dashboards/overview.json": '{"uid":"overview","title":"Overview","panels":[]}\n',
    "tn/edge/compose.yaml": "include:\n  - traefik/service.yaml\n",
    "tn/edge/.sops.env": SOPS_ENV,
    "tn/edge/env.template": "",
    "tn/edge/traefik/service.yaml": f"""
        services:
          traefik:
            command:
              - --entrypoints.https.forwardedHeaders.trustedIPs={CIDRS}
              - --entrypoints.http.forwardedHeaders.trustedIPs={CIDRS}
            labels:
              forwardedHeadersTrustedIPs: {CIDRS}
    """,
    "tools/dashboards/dashboards.py": "import sys\nsys.exit(0)\n",
    "tools/declared-containers/declared_containers.py": "import sys\nsys.exit(0)\n",
}


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip("\n"))


def commit(root: Path, message: str = "change") -> str:
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """A committed fixture repository; returns its root."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(root, BASE_FILES)
    commit(root, "base")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root.resolve()


@pytest.fixture
def validate(repo, monkeypatch):
    """The validate module, pointed at the fixture repository, with no recorded failures."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(validate_module, "failures", [])
    monkeypatch.setattr(validate_module, "ROOT", validate_module.ROOT)
    monkeypatch.setattr(validate_module, "PATHS", dict(validate_module.PATHS))
    validate_module.configure(repo)
    return validate_module


@dataclass
class DockerRun:
    image: str
    args: list[str]  # the entrypoint, then its arguments
    mounts: dict[str, str]  # container path -> file contents when it ran (or the host directory)


@dataclass
class Runtime:
    commands: list[list[str]] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    runs: list[DockerRun] = field(default_factory=list)


def docker_run_of(cmd: list[str]) -> DockerRun:
    i = cmd.index("--entrypoint")
    entrypoint, i, mounts = cmd[i + 1], i + 2, {}
    while cmd[i] == "-v":
        src, dst, _ = cmd[i + 1].rsplit(":", 2)
        mounts[dst] = Path(src).read_text() if Path(src).is_file() else src
        i += 2
    return DockerRun(cmd[i], [entrypoint, *cmd[i + 1:]], mounts)


@pytest.fixture
def runtime(validate, monkeypatch):
    """Fake only Docker and HTTP; exercise the monitoring check and real fixture files."""
    state = Runtime()
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] != "docker":
            return real_run(cmd, **kwargs)
        state.commands.append(cmd)
        if cmd[1] == "run":
            state.runs.append(docker_run_of(cmd))
        out = {
            "create": "ci-container\n", "port": "127.0.0.1:12345\n",
            "logs": "", "start": "ci-container\n",
        }.get(cmd[1], "")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def urlopen(request, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else request
        state.requests.append(url)
        if "broken_query" in url:
            raise HTTPError(url, 400, "bad query", Message(), io.BytesIO(
                b'{"status":"error","errorType":"bad_data","error":"parse error"}'))
        if "/api/datasources" in url:
            data = [{"uid": "prometheus", "type": "prometheus"}, {"uid": "loki", "type": "loki"}]
        elif "/api/search" in url:
            data = [{"uid": "overview"}]
        elif "/api/health" in url:
            data = {"database": "ok"}
        else:
            data = {"status": "success", "data": []}
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(validate.subprocess, "run", run)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return state
