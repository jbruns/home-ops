import subprocess
import sys
import textwrap
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
    return root.resolve()


@pytest.fixture
def validate(repo, monkeypatch):
    """The validate module, pointed at the fixture repository, with no recorded failures."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(validate_module, "failures", [])
    for name in ("ROOT", "HOST_DIR", "DOCO_CD_CONFIG"):
        monkeypatch.setattr(validate_module, name, getattr(validate_module, name))
    monkeypatch.setattr(validate_module, "PATHS", dict(validate_module.PATHS))
    validate_module.configure(repo)
    return validate_module
