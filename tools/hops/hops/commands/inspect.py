"""hops inspect <svc>: health-check log, last exit code, OOM kill, memory vs limit, start time, image."""
import json
import re

from hops import docker, output

HELP = "why a Service's container is in its state: health log, exit code, OOM kill, memory"
UNITS = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
         "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def register(subparsers) -> None:
    p = subparsers.add_parser("inspect", help=HELP, description=HELP + ". Read-only.")
    p.add_argument("service", help="a Service: stack/service, or a unique Service name")
    output.add_json_flag(p)
    p.set_defaults(func=run)


def parse_size(text: str) -> int | None:
    m = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]+)\s*", text)
    if not m or m[2].lower() not in UNITS:
        return None
    return round(float(m[1]) * UNITS[m[2].lower()])


def memory(runner, c: docker.Container) -> dict | None:
    if not c.running:
        return None
    stats = json.loads(runner.docker("stats", "--no-stream", "--format", "json", c.name).stdout)
    usage = parse_size(stats.get("MemUsage", "").split("/")[0])
    percent = round(100 * usage / c.memory_limit, 1) if usage is not None and c.memory_limit else None
    return {"usage_bytes": usage, "limit_bytes": c.memory_limit, "percent": percent}


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return str(n)


def run(args, ctx) -> int:
    service = ctx.declared.resolve_one(args.service)
    c = docker.find(ctx.runner, service)
    health = (c.raw.get("State") or {}).get("Health") or {}
    data = {
        "stack": service.stack,
        "service": service.name,
        "container": c.name,
        "image": c.image,
        "state": c.status,
        "health": c.health,
        "started_at": c.started_at,
        "exit_code": c.exit_code,
        "oom_killed": c.oom_killed,
        "restart_count": c.restart_count,
        "memory": memory(ctx.runner, c),
        "health_log": [{"start": e.get("Start"), "end": e.get("End"), "exit_code": e.get("ExitCode"),
                        "output": e.get("Output", "")} for e in health.get("Log") or []],
    }
    if args.json:
        output.print_json(data, ctx.stdout)
        return 0
    mem = data["memory"]
    if mem:
        limit = human_size(mem["limit_bytes"]) if mem["limit_bytes"] else "no limit"
        pct = f" ({mem['percent']}%)" if mem["percent"] is not None else ""
        mem_text = f"{human_size(mem['usage_bytes'])} / {limit}{pct}"
    else:
        mem_text = "- (not running)"
    w = ctx.stdout.write
    w(f"{service.key} ({c.name})\n")
    w(f"  image:      {c.image}\n")
    w(f"  state:      {c.status}, health {c.health or 'none'}, {c.restart_count} restarts\n")
    w(f"  started:    {c.started_at}\n")
    w(f"  exit code:  {c.exit_code}{' (OOM-killed)' if c.oom_killed else ''}\n")
    w(f"  memory:     {mem_text}\n")
    if data["health_log"]:
        w("  health log:\n")
        for e in data["health_log"]:
            w(f"    {e['start']}  exit {e['exit_code']}  {e['output'].strip()}\n")
    return 0
