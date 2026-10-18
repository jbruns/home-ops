#!/usr/bin/env python3
"""Repository validation for home-ops. Used by CI and the pre-commit hook.

Subcommands:
  secrets     every sensitive file referenced by Compose is tracked only in SOPS-encrypted form
              (doco-cd Stacks, entry file compose.yaml, reference the *.sops.* files directly),
              and no plaintext twin (x.yml beside x.sops.yml) is tracked; every file a Stack includes is
              tn/<stack>/<service>/service.yaml and some Stack includes it, and .doco-cd.yml keeps
              auto_discovery.depth 1
  env         every ${VAR} a Stack uses exists in its env.template and its encrypted .sops.env
  compose     `docker compose config` succeeds for every Stack and templates/* with placeholder values;
              every Service has a startup-order label, json-file logging and restart: unless-stopped,
              and every Exposed Service (https router) a probe.url. A Service that extends
              tn/_baseline.yaml meets the Baseline or carries a baseline.exempt.<item> reason, and its
              routers follow ADR 0006 (names, health and Client Paths, Protected Probes)
  isolation   tn/doco-cd and tn/_baseline.yaml changes ship in their own PR
  monitoring  promtool / amtool / loki / alloy checks, using the image versions the Stacks deploy;
              Grafana dashboards JSON matches its generator (tools/dashboards/dashboards.py)
  cloudflare  the Cloudflare ranges Traefik trusts are valid and identical everywhere they appear

Usage: validate.py [--root DIR] [--path NAME=PATH ...] [check ...]   (no check: run them all)
  --root  the repository to check (default: the one this file is in)
  --path  override one of PATHS below, relative to --root or absolute
"""
import argparse
import os
import re
import shutil
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

TOOLS_DIR = Path(__file__).resolve().parents[1]
ROOT = TOOLS_DIR.parent
HOST_DIR = ROOT / "tn"
# Files and folders individual checks read, relative to ROOT.
PATHS = {
    "prometheus": "tn/monitoring/prometheus",
    "alertmanager": "tn/monitoring/alertmanager",
    "loki": "tn/monitoring/loki",
    "alloy": "tn/monitoring/alloy",
    "blackbox": "tn/monitoring/blackbox-exporter/blackbox.yml",
    "dashboards": "tools/dashboards/dashboards.py",
    "traefik": "tn/edge/traefik/service.yaml",
}
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


# doco-cd auto-discovers a Stack only by tn/<stack>/compose.yaml (see .doco-cd.yml).
ENTRY_NAME = "compose.yaml"
# Each file a Stack's entry file includes: tn/<stack>/<service>/service.yaml, one Service plus its sidecars.
INCLUDED_NAME = "service.yaml"
DOCO_CD_CONFIG = ROOT / ".doco-cd.yml"


def configure(root: Path, paths: dict[str, str] | None = None) -> None:
    """Point every check at another repository root and/or override entries of PATHS."""
    global ROOT, HOST_DIR, DOCO_CD_CONFIG
    ROOT = root.resolve()
    HOST_DIR = ROOT / "tn"
    DOCO_CD_CONFIG = ROOT / ".doco-cd.yml"
    for name, value in (paths or {}).items():
        if name not in PATHS:
            raise KeyError(f"unknown path {name!r}; choose from {', '.join(PATHS)}")
        PATHS[name] = value


def path(name: str) -> Path:
    return ROOT / PATHS[name]


def stacks(base: Path | None = None) -> list[Path]:
    return sorted(p.parent for p in ((base or ROOT) / "tn").glob(f"*/{ENTRY_NAME}"))


def entry(stack: Path) -> Path:
    return stack / ENTRY_NAME


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
            twin = f.with_name(f.name.replace(".sops.", ".", 1))
            if twin != f and twin in tracked:
                fail(f"plaintext twin of a SOPS file is tracked: {_rel(twin)}", f)
        elif f.name == ".env" or f.suffix == ".env":
            fail("plaintext .env file is tracked; encrypt it as *.sops.env", f)
    check_layout(tracked)
    for stack in stacks():
        check_stack_secrets(stack, tracked)
    print("secrets ok" if not failures else "secrets: problems found")


def check_layout(tracked: set[Path]) -> None:
    """A Stack is tn/<stack>/compose.yaml; each file it includes is <service>/service.yaml."""
    included: set[Path] = set()
    for stack in stacks():
        for f in compose_files(entry(stack))[1:]:
            included.add(f)
            if f.name != INCLUDED_NAME or f.parent.parent != stack:
                fail(f"{stack.name}: included file {_rel(f)} must be {_rel(stack)}/<service>/{INCLUDED_NAME}", entry(stack))
    for f in sorted(tracked):
        if not f.is_relative_to(HOST_DIR):
            continue
        if re.fullmatch(r"docker-compose\.ya?ml", f.name):
            fail(f"rename to {INCLUDED_NAME} (one Service per file) or {ENTRY_NAME} (a Stack's entry file)", f)
        elif f.name == INCLUDED_NAME and f.parent.parent == HOST_DIR:
            fail(f"doco-cd never discovers this; a Stack's entry file must be {ENTRY_NAME}", f)
        elif f.name == INCLUDED_NAME and f not in included:
            fail(f"no Stack's include: lists this {INCLUDED_NAME}, so it is never deployed", f)
    # doco-cd scans tn/ and each Stack folder, never deeper while depth is 1 (kimdre/doco-cd
    # internal/config/deploy/auto_discovery.go). It only looks for compose.yaml, so service.yaml is
    # never discovered; this is defence in depth: at depth 0 (unlimited) a compose.yaml in a Service
    # folder would become a Stack, and delete: true would remove it when it disappears.
    depth = ((yaml.safe_load(DOCO_CD_CONFIG.read_text()) or {}).get("auto_discovery") or {}).get("depth")
    if depth != 1:
        fail(f"auto_discovery.depth must stay 1 (found {depth!r}): deeper discovery would deploy a compose.yaml below a Stack folder as a Stack of its own", DOCO_CD_CONFIG)


def check_stack_secrets(stack: Path, tracked: set[Path]) -> None:
    # doco-cd reads interpolation values from .sops.env (env_files in .doco-cd.yml) and fails without it.
    if stack / ".sops.env" not in tracked:
        fail(f"{stack.name}: Stack has no tracked .sops.env", entry(stack))
    for compose in compose_files(entry(stack)):
        for ref in sensitive_refs(compose):
            if not ref.is_relative_to(ROOT):
                continue  # Host path outside the repo, e.g. the age key
            if ref not in tracked:
                fail(f"referenced file {_rel(ref)} is not tracked", compose)
            elif ".sops." not in ref.name:
                fail(f"sensitive file {_rel(ref)} must be SOPS-encrypted and named *.sops.*", compose)


def stack_vars(stack: Path) -> set[str]:
    used: set[str] = set()
    for compose in compose_files(entry(stack)):
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
    check_dependencies_env()


def check_dependencies_env() -> None:
    """dependencies-render fails on any placeholder in dependencies.yml its environment lacks."""
    prom = path("prometheus")
    template = prom / "dependencies.yml"
    svc = yaml.safe_load((prom / INCLUDED_NAME).read_text())["services"]["dependencies-render"]
    passed = {e.split("=", 1)[0] for e in svc.get("environment") or []}
    if missing := sorted(set(VAR_RE.findall(template.read_text())) - passed):
        fail(f"dependencies.yml placeholders not passed to dependencies-render: {', '.join(missing)}", template)


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


STARTUP_LABEL = "startup-order"
STARTUP_LEVELS = {"0", "1", "2", "3"}
# json-file rotation for every service; the engine default keeps logs forever.
LOGGING = {"driver": "json-file", "max-size": "10m", "max-file": "3"}
HTTPS_RULE_RE = re.compile(r"^traefik\.http\.routers\..+-https\.rule$")

# The Baseline (ADR 0007). Every Service `extends` tn/_baseline.yaml, except doco-cd's, which carry
# the same items inline (extending would race doco-cd's own self-update); both are held to it.
BASELINE_FILE = "tn/_baseline.yaml"
TEMPLATES_DIR = "templates"
EXEMPT_PREFIX = "baseline.exempt."
NNP_RE = re.compile(r"^no-new-privileges([=:]true)?$")
BASELINE_ITEMS = {
    "healthcheck": lambda s: bool((s.get("healthcheck") or {}).get("test")) and not (s.get("healthcheck") or {}).get("disable"),
    "no-new-privileges": lambda s: any(NNP_RE.match(str(o)) for o in s.get("security_opt") or []),
    "digest": lambda s: "@sha256:" in str(s.get("image", "")),
    "memory": lambda s: bool(((s.get("deploy") or {}).get("resources") or {}).get("limits", {}).get("memory") or s.get("mem_limit")),
    "cap_drop": lambda s: "ALL" in (s.get("cap_drop") or []),
}
# Router names carry their kind, so recording rules can tell UI, health and Client Path apart (ADR 0006).
ROUTER_LABEL_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.")
SERVICE_LABEL_RE = re.compile(r"^traefik\.http\.services\.([^.]+)\.")
PROTECTED_CHAIN = "protected"
FORWARD_AUTH = "idp-forward-auth"


def baseline_services(stack: Path, base: Path) -> set[str]:
    """Services whose own definition extends the Baseline file."""
    target = (base / BASELINE_FILE).resolve()
    names = set()
    for compose in compose_files(entry(stack)):
        for name, svc in ((yaml.safe_load(compose.read_text()) or {}).get("services") or {}).items():
            ext = (svc or {}).get("extends")
            if isinstance(ext, dict) and ext.get("file") and (compose.parent / ext["file"]).resolve() == target:
                names.add(name)
    return names


def middlewares(labels: dict, router: str) -> list[str]:
    raw = str(labels.get(f"traefik.http.routers.{router}.middlewares", ""))
    return [m.strip().split("@")[0] for m in raw.split(",") if m.strip()]


def baseline_problems(name: str, svc: dict) -> tuple[list[str], list[str]]:
    """Problems with one Baseline Service, and its Exemptions for the audit report."""
    labels = svc.get("labels") or {}
    problems, exemptions = [], []
    for key, reason in labels.items():
        if key.startswith(EXEMPT_PREFIX) and key[len(EXEMPT_PREFIX):] not in BASELINE_ITEMS:
            problems.append(f"{name}: {key} names no Baseline item ({', '.join(BASELINE_ITEMS)})")
    for item, met in BASELINE_ITEMS.items():
        reason = str(labels.get(EXEMPT_PREFIX + item, "")).strip()
        if met(svc) and EXEMPT_PREFIX + item in labels:
            problems.append(f"{name}: stale Exemption {EXEMPT_PREFIX}{item}; the Service meets it")
        elif not met(svc) and not reason:
            problems.append(f"{name}: misses Baseline item {item!r} without a {EXEMPT_PREFIX}{item} reason")
        elif not met(svc):
            exemptions.append(f"{name}: {item}: {reason}")
    return problems + router_problems(svc.get("container_name") or name, labels), exemptions


def router_problems(name: str, labels: dict) -> list[str]:
    """`name` is the container name: Traefik metrics are mapped back to containers by it."""
    problems = []
    routers = {m.group(1) for k in labels if (m := ROUTER_LABEL_RE.match(k))}
    kind_re = re.compile(rf"^{re.escape(name)}-(http|https|health|client-[a-z0-9]+)$")
    for r in sorted(routers):
        if not kind_re.match(r):
            problems.append(f"{name}: router {r!r} must be {name}-http|-https|-health|-client-<protocol>")
            continue
        rule, mws = str(labels.get(f"traefik.http.routers.{r}.rule", "")), middlewares(labels, r)
        client, health = "-client-" in r, r.endswith("-health")
        if "Header" in rule and not client:
            problems.append(f"{name}: router {r} matches on a header; only Client Paths may")
        if health and ("PathPrefix(" in rule or "Path(" not in rule):
            problems.append(f"{name}: router {r} must match one exact Path()")
        if (client or health) and {PROTECTED_CHAIN, FORWARD_AUTH} & set(mws):
            problems.append(f"{name}: router {r} must not use forward auth")
        if client and ("crowdsec-bouncer" not in mws or not any("ratelimit" in m for m in mws)):
            problems.append(f"{name}: Client Path {r} needs crowdsec-bouncer and a rate limit")
    if any({PROTECTED_CHAIN, FORWARD_AUTH} & set(middlewares(labels, r)) for r in routers):
        if not str(labels.get("probe.idp_url", "")).startswith("https://"):
            problems.append(f"{name}: a Protected Service needs a probe.idp_url label")
        if f"{name}-health" not in routers:
            problems.append(f"{name}: a Protected Service needs a {name}-health router for its Probe")
    for s in sorted({m.group(1) for k in labels if (m := SERVICE_LABEL_RE.match(k))} - {name}):
        problems.append(f"{name}: Traefik service {s!r} must be named {name!r} (Http5xxRatio maps it to the container)")
    return problems


def probe_modules() -> set[str]:
    return set(yaml.safe_load(path("blackbox").read_text())["modules"])


def probe_problems(name: str, labels: dict, modules: set[str]) -> list[str]:
    """An Exposed Service (an https router) needs a Probe (GLOSSARY.md, prometheus.yml)."""
    problems = []
    if any(HTTPS_RULE_RE.match(k) for k in labels) and not str(labels.get("probe.url", "")).startswith("https://"):
        problems.append(f"{name}: has an https router but no probe.url label")
    if "probe.module" in labels and labels["probe.module"] not in modules:
        problems.append(f"{name}: probe.module {labels['probe.module']!r} is not a blackbox.yml module")
    return problems


def check_compose() -> None:
    modules = probe_modules()
    exemptions: list[str] = []
    counts = {"baseline": 0, "total": 0}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp).resolve()
        for f in tracked_files():
            dst = work / f.relative_to(ROOT)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
        # Each template is rendered as if it were a Stack, so it stays valid against the Baseline.
        for tpl in sorted((work / TEMPLATES_DIR).glob(f"*/{ENTRY_NAME}")):
            shutil.copytree(tpl.parent, work / "tn" / f"_template-{tpl.parent.name}")
        for stack in stacks(work):
            keys = env_keys(stack / "env.template") | stack_vars(stack)
            (stack / ".env").write_text("".join(f"{k}={placeholder(k)}\n" for k in sorted(keys)))
            for compose in compose_files(entry(stack)):
                for ref in sensitive_refs(compose):
                    if ref.is_relative_to(work) and not ref.exists():
                        ref.write_text("")
            r = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=stack, capture_output=True, text=True)
            noise = [line for line in r.stderr.splitlines() if line.strip()]
            template = stack.name.startswith("_template-")
            src = (ROOT / TEMPLATES_DIR / stack.name.removeprefix("_template-") if template else ROOT / "tn" / stack.name) / entry(stack).name
            if r.returncode != 0 or any("is not set" in line for line in noise):
                fail(f"{stack.name}: docker compose config failed:\n" + "\n".join(noise), src)
                continue
            services = json.loads(r.stdout).get("services", {})
            bad = [name for name, svc in services.items()
                   if (svc.get("labels") or {}).get(STARTUP_LABEL) not in STARTUP_LEVELS]
            probes = [p for name, svc in services.items() for p in probe_problems(name, svc.get("labels") or {}, modules)]
            logs = [name for name, svc in services.items()
                    if {"driver": (svc.get("logging") or {}).get("driver"),
                        **{k: (svc.get("logging") or {}).get("options", {}).get(k) for k in ("max-size", "max-file")}} != LOGGING]
            restart = [name for name, svc in services.items() if svc.get("restart") != "unless-stopped"]
            on_baseline = baseline_services(stack, work)
            if template and set(services) - on_baseline:
                fail(f"{stack.name}: every template Service must extend {BASELINE_FILE}", src)
            if stack.name == "doco-cd" and on_baseline:
                fail(f"doco-cd must not extend {BASELINE_FILE} (ADR 0007); keep its settings inline", src)
            inline = set(services) if stack.name == "doco-cd" else set()
            base_problems = [f"{name}: must extend {BASELINE_FILE}" for name in sorted(set(services) - on_baseline - inline)]
            for name in sorted((on_baseline | inline) & set(services)):
                p, e = baseline_problems(name, services[name])
                base_problems += p
                exemptions += [f"{stack.name}/{x}" for x in e]
            if not template:
                counts["baseline"] += len((on_baseline | inline) & set(services))
                counts["total"] += len(services)
            if bad:
                fail(f"{stack.name}: services without a valid {STARTUP_LABEL} label (0-3, ADR-0003): {', '.join(sorted(bad))}", src)
            if probes:
                fail(f"{stack.name}: " + "; ".join(probes), src)
            if logs:
                fail(f"{stack.name}: services without json-file logging at max-size 10m, max-file 3: {', '.join(sorted(logs))}", src)
            if restart:
                fail(f"{stack.name}: services without restart: unless-stopped: {', '.join(sorted(restart))}", src)
            if base_problems:
                fail(f"{stack.name}: Baseline (ADR 0007): " + "; ".join(base_problems), src)
            if not bad and not probes and not logs and not restart and not base_problems:
                print(f"compose ok: {stack.name}")
    print(f"Baseline: {counts['baseline']} of {counts['total']} Services meet it (ADR 0007)")
    for x in exemptions:
        print(f"  Exemption {x}")


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
    prom = path("prometheus")
    prom_image = deployed_image(prom / INCLUDED_NAME, "prometheus")
    rule_files = sorted(prom.glob("*rules*.yml"))
    prom_mounts = {f: f"/etc/prometheus/{f.name}" for f in [prom / "prometheus.yml", *rule_files]}
    docker_run(prom_image, prom_mounts, "promtool", "check", "config", "--syntax-only", "/etc/prometheus/prometheus.yml")
    docker_run(prom_image, prom_mounts, "promtool", "check", "rules", *[f"/etc/prometheus/{f.name}" for f in rule_files])

    am = path("alertmanager")
    if (am / "config.yml") in tracked_files():
        am_image = deployed_image(am / INCLUDED_NAME, "alertmanager")
        docker_run(am_image, {am / "config.yml": "/etc/alertmanager/config.yml"}, "amtool", "check-config", "/etc/alertmanager/config.yml")
    else:
        warn("alertmanager config is encrypted-only; amtool check skipped until it is split from its secrets")

    loki = path("loki")
    loki_image = deployed_image(loki / INCLUDED_NAME, "loki")
    docker_run(loki_image, {loki / "loki.yaml": "/etc/loki/loki.yaml"}, "/usr/bin/loki", "-config.file=/etc/loki/loki.yaml", "-verify-config")

    alloy = path("alloy")
    alloy_image = deployed_image(alloy / INCLUDED_NAME, "alloy")
    docker_run(alloy_image, {alloy / "config.alloy": "/etc/alloy/config.alloy"}, "/bin/alloy", "validate", "/etc/alloy/config.alloy")

    generator = path("dashboards")
    result = subprocess.run([sys.executable, str(generator), "--check"], capture_output=True, text=True)
    if result.returncode:
        fail(result.stdout.strip() or result.stderr.strip(), generator)
    else:
        print(f"ok: grafana dashboards match {generator.name}")


def check_cloudflare() -> None:
    sys.path.insert(0, str(TOOLS_DIR / "cloudflare-ips"))
    import cloudflare_ips

    traefik = path("traefik")
    errs = cloudflare_ips.problems(traefik.read_text())
    for err in errs:
        fail(err, traefik)
    if not errs:
        print("cloudflare ok")


def check_isolation() -> None:
    """A change to the doco-cd Stack ships alone: doco-cd self-updates mid-Deploy, so other
    Stacks in the same commit deploy across the handover. A change to the Baseline ships without
    Stack changes: it recreates every Service that extends it, so its Deploy should be about it
    alone (ADR 0007). Compares against VALIDATE_BASE (CI sets the PR base) or origin/main."""
    base = os.environ.get("VALIDATE_BASE") or "origin/main"
    r = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"isolation: cannot diff against {base}: {r.stderr.strip()}")
        return
    stacks_changed = {Path(f).parts[1] for f in r.stdout.split() if Path(f).parts[:1] == ("tn",) and len(Path(f).parts) > 2}
    if "doco-cd" in stacks_changed and len(stacks_changed) > 1:
        others = ", ".join(sorted(stacks_changed - {"doco-cd"}))
        fail(f"isolation: tn/doco-cd changes must ship in their own PR (also changed: {others})")
    elif BASELINE_FILE in r.stdout.split() and stacks_changed:
        fail(f"isolation: {BASELINE_FILE} changes must ship in their own PR (also changed: {', '.join(sorted(stacks_changed))})")
    else:
        print("isolation ok")


CHECKS = {
    "secrets": check_secrets,
    "env": check_env,
    "compose": check_compose,
    "monitoring": check_monitoring,
    "cloudflare": check_cloudflare,
    "isolation": check_isolation,
}

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Repository validation for home-ops.")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository to check")
    parser.add_argument("--path", action="append", default=[], metavar="NAME=PATH",
                        help=f"override a path ({', '.join(PATHS)})")
    parser.add_argument("checks", nargs="*", metavar="check", help=f"{', '.join(CHECKS)} (default: all)")
    args = parser.parse_args(argv)
    overrides = dict(p.split("=", 1) for p in args.path if "=" in p)
    if len(overrides) != len(args.path) or set(overrides) - set(PATHS):
        parser.error(f"--path takes NAME=PATH with NAME one of {', '.join(PATHS)}")
    configure(args.root, overrides)
    for name in args.checks or list(CHECKS):
        if name not in CHECKS:
            sys.exit(f"unknown check {name!r}; choose from {', '.join(CHECKS)}")
        CHECKS[name]()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
