#!/usr/bin/env python3
"""Repository validation for home-ops. Used by CI and the pre-commit hook.

Subcommands:
  secrets     every sensitive file referenced by Compose is tracked only in SOPS-encrypted form
  env         every ${VAR} a Stack uses exists in its env.template and its encrypted .sops.env
  compose     `docker compose config` succeeds for every Stack with placeholder values
  monitoring  promtool / amtool / loki checks, using the image versions the Stacks deploy
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HOST_DIR = ROOT / "tn"
# ${VAR} but not $${VAR} (Compose's escape for a literal "$")
VAR_RE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)")
SOPS_MARKERS = ("sops_version=", '"sops":', "sops:")

failures: list[str] = []


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def fail(msg: str, file: Path | None = None) -> None:
    failures.append(msg)
    if os.environ.get("GITHUB_ACTIONS"):
        loc = f" file={_rel(file)}" if file else ""
        print(f"::error{loc}::{msg}".replace("\n", "%0A"))
    else:
        print(f"ERROR: {msg}")


def warn(msg: str) -> None:
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"WARN: {msg}")


def tracked_files() -> set[Path]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT, text=True)
    return {ROOT / p for p in out.split("\0") if p and (ROOT / p).is_file()}


def stacks(base: Path = ROOT) -> list[Path]:
    return sorted(p.parent for p in (base / "tn").glob("*/docker-compose.yaml"))


def compose_files(entry: Path) -> list[Path]:
    """The entry Compose file plus everything it includes, recursively."""
    seen: list[Path] = []
    todo = [entry]
    while todo:
        f = todo.pop()
        if f in seen or not f.exists():
            continue
        seen.append(f)
        doc = yaml.safe_load(f.read_text()) or {}
        for inc in doc.get("include") or []:
            path = inc.get("path") if isinstance(inc, dict) else inc
            for p in [path] if isinstance(path, str) else (path or []):
                todo.append((f.parent / p).resolve())
    return seen


def sensitive_refs(compose: Path) -> list[Path]:
    """Files a Compose file expects to exist, decrypted, on the Host."""
    doc = yaml.safe_load(compose.read_text()) or {}
    refs: list[str] = []
    for svc in (doc.get("services") or {}).values():
        env_file = svc.get("env_file")
        for e in [env_file] if isinstance(env_file, (str, dict)) else (env_file or []):
            refs.append(e["path"] if isinstance(e, dict) else e)
        for v in svc.get("volumes") or []:
            if isinstance(v, dict) and str(v.get("x-sops")).lower() == "true":
                refs.append(v["source"])
    for sec in (doc.get("secrets") or {}).values():
        if isinstance(sec, dict) and sec.get("file"):
            refs.append(sec["file"])
    return [(compose.parent / r).resolve() for r in refs]


def encrypted_name(p: Path) -> Path:
    # stack-back.env -> stack-back.sops.env ; config.yml -> config.sops.yml
    return p.with_name(f"{p.stem}.sops{p.suffix}")


def env_keys(path: Path) -> set[str]:
    keys = set()
    if not path.exists():
        return keys
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            if not k.startswith("sops_"):
                keys.add(k)
    return keys


def check_secrets() -> None:
    tracked = tracked_files()
    for f in sorted(tracked):
        if ".sops." in f.name or f.name == ".sops.env":
            if f.name == ".sops.yaml":
                continue
            if not any(m in f.read_text(errors="ignore") for m in SOPS_MARKERS):
                fail("file is named *.sops.* but is not SOPS-encrypted", f)
        elif f.name == ".env" or f.suffix == ".env":
            fail("plaintext .env file is tracked; encrypt it as *.sops.env", f)
    for stack in stacks():
        for compose in compose_files(stack / "docker-compose.yaml"):
            for ref in sensitive_refs(compose):
                if ref in tracked:
                    fail(f"sensitive file {_rel(ref)} is tracked in plaintext", compose)
                if encrypted_name(ref) not in tracked:
                    fail(f"no encrypted counterpart {_rel(encrypted_name(ref))} is tracked", compose)
    print("secrets ok" if not failures else "secrets: problems found")


def stack_vars(stack: Path) -> set[str]:
    used: set[str] = set()
    for compose in compose_files(stack / "docker-compose.yaml"):
        used |= set(VAR_RE.findall(compose.read_text()))
    return used


def check_env() -> None:
    for stack in stacks():
        used = stack_vars(stack)
        template, encrypted = stack / "env.template", stack / ".sops.env"
        tpl, enc = env_keys(template), env_keys(encrypted)
        if missing := sorted(used - tpl):
            fail(f"{stack.name}: variables used but missing from env.template: {', '.join(missing)}", template)
        if missing := sorted(used - enc):
            fail(f"{stack.name}: variables used but missing from .sops.env (deploy would get blanks): {', '.join(missing)}", encrypted)
        if unused := sorted(tpl - used):
            warn(f"{stack.name}: env.template keys no Compose file uses: {', '.join(unused)}")
        print(f"env checked: {stack.name}")


def placeholder(key: str) -> str:
    k = key.upper()
    if "SUBNET" in k:
        return "10.99.0.0/24"
    if "IPV4" in k:
        return "10.99.0.1"
    if k.endswith(("UID", "GID", "PORT")):
        return "1000"
    if k.startswith(("ZFS_POOL_ROOT", "DOCKER_DATA_ROOT")):
        return "/mnt/placeholder"
    return "placeholder"


def check_compose() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for f in tracked_files():
            dst = work / f.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
        for stack in stacks(work):
            keys = env_keys(stack / "env.template") | stack_vars(stack)
            (stack / ".env").write_text("".join(f"{k}={placeholder(k)}\n" for k in sorted(keys)))
            for compose in compose_files(stack / "docker-compose.yaml"):
                for ref in sensitive_refs(compose):
                    if not ref.exists():
                        ref.write_text("")
            r = subprocess.run(["docker", "compose", "config", "-q"], cwd=stack, capture_output=True, text=True)
            noise = [line for line in r.stderr.splitlines() if line.strip()]
            if r.returncode != 0 or any("is not set" in line for line in noise):
                fail(f"{stack.name}: docker compose config failed:\n" + "\n".join(noise), ROOT / "tn" / stack.name / "docker-compose.yaml")
            else:
                print(f"compose ok: {stack.name}")


def deployed_image(compose: Path, service: str) -> str:
    return yaml.safe_load(compose.read_text())["services"][service]["image"]


def docker_run(image: str, mounts: dict[Path, str], entrypoint: str, *args: str) -> None:
    cmd = ["docker", "run", "--rm", "--entrypoint", entrypoint]
    for src, dst in mounts.items():
        cmd += ["-v", f"{src}:{dst}:ro"]
    cmd += [image, *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    label = f"{entrypoint} {' '.join(args)}"
    if r.returncode != 0:
        fail(f"{label} failed:\n{out}")
    else:
        print(f"ok: {label}")


def check_monitoring() -> None:
    mon = HOST_DIR / "monitoring"
    prom = mon / "prometheus"
    prom_image = deployed_image(prom / "docker-compose.yaml", "prometheus")
    rule_files = sorted(prom.glob("*rules*.yml"))
    prom_mounts = {f: f"/etc/prometheus/{f.name}" for f in [prom / "prometheus.yml", *rule_files]}
    docker_run(prom_image, prom_mounts, "promtool", "check", "config", "--syntax-only", "/etc/prometheus/prometheus.yml")
    docker_run(prom_image, prom_mounts, "promtool", "check", "rules", *[f"/etc/prometheus/{f.name}" for f in rule_files])

    am = mon / "alertmanager"
    if (am / "config.yml") in tracked_files():
        am_image = deployed_image(am / "docker-compose.yaml", "alertmanager")
        docker_run(am_image, {am / "config.yml": "/etc/alertmanager/config.yml"}, "amtool", "check-config", "/etc/alertmanager/config.yml")
    else:
        warn("alertmanager config is encrypted-only; amtool check skipped until it is split from its secrets")

    loki = mon / "loki"
    loki_image = deployed_image(loki / "docker-compose.yaml", "loki")
    docker_run(loki_image, {loki / "loki.yaml": "/etc/loki/loki.yaml"}, "/usr/bin/loki", "-config.file=/etc/loki/loki.yaml", "-verify-config")


CHECKS = {"secrets": check_secrets, "env": check_env, "compose": check_compose, "monitoring": check_monitoring}

if __name__ == "__main__":
    selected = sys.argv[1:] or list(CHECKS)
    for name in selected:
        if name not in CHECKS:
            sys.exit(f"unknown check {name!r}; choose from {', '.join(CHECKS)}")
        CHECKS[name]()
    sys.exit(1 if failures else 0)
