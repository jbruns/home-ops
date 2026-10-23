"""hops host: the Host's pools, key datasets, Docker disk usage and task status (read-only)."""
import json
import re

from hops import config, docker, hygiene, output, snapshots, truenas
from hops.errors import ConfigError, HopsError

HELP = ("the Host's pools, key datasets, Docker disk usage, Rollback Snapshots, and snapshot and "
        "Cloud Sync task status")
SETTING = "HOPS_HOST_DATASETS"
# stack-back writes the restic repository to a bind mount; its image names the Service in any Stack.
RESTIC_IMAGE = "lawndoc/stack-back"
# Where that mount is in the container: RESTIC_REPOSITORY, which is encrypted in
# tn/ops/stack-back.sops.env, so it is named here. Keep in step with tn/ops/stack-back/service.yaml.
RESTIC_MOUNT = "/backups"
DOCKER_TYPES = {"Images": "images", "Containers": "containers", "Local Volumes": "volumes",
                "Build Cache": "build_cache"}
# docker system df prints sizes with go-units' decimal suffixes, e.g. 61.48GB, 512kB, 0B.
SIZE = re.compile(r"^\s*([\d.]+)\s*([kMGTP]?)B")
PERCENT = re.compile(r"\((\d+)%\)")
UNITS = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15}


def register(subparsers) -> None:
    p = subparsers.add_parser("host", help=HELP, description=(
        HELP + ". Read-only. Datasets: the one holding Docker's data-root, the restic repository's, and "
        f"those listed in {SETTING}. Without TrueNAS configured, only Docker usage is shown."))
    output.add_json_flag(p)
    p.set_defaults(func=run)


def parse_size(text: str | None) -> int | None:
    m = SIZE.match(text or "")
    return round(float(m.group(1)) * UNITS[m.group(2)]) if m else None


def count(text) -> int | None:
    return int(text) if str(text).isdigit() else None


def docker_usage(runner) -> list[dict]:
    out = []
    for line in runner.docker("system", "df", "--format", "json").stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        percent = PERCENT.search(row.get("Reclaimable") or "")
        out.append({
            "type": DOCKER_TYPES.get(row.get("Type"), row.get("Type")),
            "total": count(row.get("TotalCount")), "active": count(row.get("Active")),
            "size": row.get("Size"), "size_bytes": parse_size(row.get("Size")),
            "reclaimable": row.get("Reclaimable"), "reclaimable_bytes": parse_size(row.get("Reclaimable")),
            "reclaimable_percent": int(percent.group(1)) if percent else None,
        })
    return out


def failed(exc: HopsError) -> str:
    """A note's reason for a failed call. Never the error's text: TrueNAS can echo task scripts,
    arguments or credentials in it."""
    method = getattr(exc, "method", None)
    return f"TrueNAS read failed ({method})" if method else "TrueNAS read failed"


def restic_paths(ctx, notes: list[str]) -> list[str]:
    """Where stack-back's container keeps the restic repository: its bind mount at RESTIC_MOUNT."""
    services = [s for s in ctx.declared.services if s.image and s.image.repository.endswith(RESTIC_IMAGE)]
    if not services:
        notes.append(f"no Stack declares {RESTIC_IMAGE}; the restic dataset is not shown")
        return []
    paths = []
    for service in services:
        try:
            found = docker.find(ctx.runner, service)
        except HopsError:
            notes.append(f"restic dataset unknown: no container found for {service.key}")
            continue
        source = found.bind_source(RESTIC_MOUNT)
        if source:
            paths.append(source)
        else:
            notes.append(f"restic dataset unknown: {service.key} has no bind mount at {RESTIC_MOUNT}")
    return paths


def datasets(ctx, client, notes: list[str]) -> list[dict]:
    roles: dict[str, list[str]] = {}

    def add(name, role):
        if name and role not in roles.setdefault(name, []):
            roles[name].append(role)

    try:
        root = json.loads(ctx.runner.docker("info", "--format", "{{json .DockerRootDir}}").stdout or "null")
    except HopsError:
        root = None
        notes.append("docker dataset unknown: docker info failed")
    wanted = [(root, "docker")] if root else []
    wanted += [(path, "restic") for path in restic_paths(ctx, notes)]
    for path, role in wanted:
        try:
            name = hygiene.dataset_at(client, path)
        except truenas.TrueNASError as exc:
            notes.append(f"{role} dataset unknown: {failed(exc)}")
            continue
        if name:
            add(name, role)
        else:
            notes.append(f"{role} path {path} is not on a ZFS dataset")
    for name in re.split(r"[\s,]+", config.load_values(ctx.repo_root, ctx.env).get(SETTING) or ""):
        add(name, "configured")
    out = []
    for name, role_list in roles.items():
        try:
            out.append({"name": name, "roles": role_list, **hygiene.dataset_usage(client, name)})
        except truenas.TrueNASError as exc:
            notes.append(f"dataset {name} skipped: {failed(exc)}")
    return out


def rollback_summary(client) -> dict:
    found = snapshots.rollback_snapshots(client)
    return {"count": len(found), "size_bytes": sum(s.size or 0 for s in found),
            "expired": [s.to_json() for s in found if s.expired]}


def yes_no(value: bool | None) -> str | None:
    return None if value is None else "yes" if value else "no"


def human(n: int | None) -> str | None:
    if n is None:
        return None
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def run(args, ctx) -> int:
    notes: list[str] = []
    report = {"pools": None, "datasets": None, "docker": docker_usage(ctx.runner), "rollback_snapshots": None,
              "tasks": None, "notes": notes}
    try:
        client = ctx.truenas
    except ConfigError as exc:  # truenas.Unavailable, or a malformed TRUENAS_URL: hops's own text
        client = None
        notes.append(str(exc))
    sections = {"pools": hygiene.pools, "datasets": lambda c: datasets(ctx, c, notes),
                "rollback_snapshots": rollback_summary, "tasks": hygiene.tasks}
    for key, read in sections.items() if client is not None else ():
        try:
            report[key] = read(client)
        except truenas.TrueNASError as exc:  # this read failed; the others may not
            notes.append(f"{key}: {failed(exc)}")
        except HopsError:  # TrueNAS unreachable: skip the rest rather than wait on each
            notes.append(f"{key} and the sections after it: TrueNAS unreachable")
            break
    if args.json:
        output.print_json(report, ctx.stdout)
    else:
        show(report, ctx.stdout)
    return 0


def show(report: dict, out) -> None:
    sections = []
    if report["pools"] is not None:
        sections.append(("Pools", None, ["POOL", "STATUS", "HEALTHY", "SIZE", "ALLOCATED", "FREE", "USED"], [
            [p["name"], p["status"], yes_no(p["healthy"]), human(p["size_bytes"]), human(p["allocated_bytes"]), human(p["free_bytes"]),
             None if p["used_percent"] is None else f"{p['used_percent']}%"] for p in report["pools"]]))
    if report["datasets"] is not None:
        sections.append(("Datasets", None, ["DATASET", "ROLE", "USED", "AVAILABLE", "SNAPSHOTS", "QUOTA"], [
            [d["name"], ",".join(d["roles"]), human(d["used_bytes"]), human(d["available_bytes"]),
             human(d["snapshots_bytes"]), human(d["quota_bytes"])] for d in report["datasets"]]))
    sections.append(("Docker", None, ["TYPE", "TOTAL", "ACTIVE", "SIZE", "RECLAIMABLE"], [
        [d["type"], d["total"], d["active"], d["size"], d["reclaimable"]] for d in report["docker"]]))
    rollback = report["rollback_snapshots"]
    if rollback is not None:
        summary = (f"{rollback['count']} Rollback Snapshots, {human(rollback['size_bytes'])}; "
                 f"{len(rollback['expired'])} expired" + (":" if rollback["expired"] else ""))
        sections.append(("Rollback Snapshots", summary, ["NAME", "ISSUE", "EXPIRES", "SIZE"], [
            [s["name"], s["issue"], s["expires"], human(s["size"])] for s in rollback["expired"]]))
    if report["tasks"] is not None:
        sections.append(("Tasks", None, ["KIND", "ID", "NAME", "SOURCE", "ENABLED", "LAST RUN", "STARTED", "FINISHED"], [
            [t["kind"], t["id"], t["name"], t["source"], yes_no(t["enabled"]),
             *([t["last_run"]["state"], t["last_run"]["started_at"], t["last_run"]["finished_at"]]
               if t["last_run"] else [None, None, None])] for t in report["tasks"]]))
    for i, (title, summary, headers, rows) in enumerate(sections):
        out.write(("\n" if i else "") + title + "\n")
        if summary:
            out.write(summary + "\n")
        if rows or not summary:
            output.table(headers, rows, out)
    for note in report["notes"]:
        out.write(f"\nnote: {note}\n")
