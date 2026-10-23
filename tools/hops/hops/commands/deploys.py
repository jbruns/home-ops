"""hops deploys [<stack> | <run-id>]: doco-cd's recent runs, from its REST API (read-only)."""
import re
from urllib.parse import quote

from hops import config, doco, output
from hops.errors import UsageError

HELP = "doco-cd's recent runs: when, trigger, status, the Stacks each touched, and why one failed or skipped"
STATUSES = ["failed", "succeeded", "running", "skipped", "accepted"]
RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
FETCH = 200  # doco-cd's maximum page; Stacks are filtered here, so fetch everything it keeps
NOTE = ("doco-cd keeps run history in memory only: 7 days, at most 50 runs per trigger, "
        "lost when doco-cd restarts. For older history: hops logs --loki doco-cd")
DEFAULT_LIMIT = 20
WIDE = 3  # more Stacks than this are shown as a count


def register(subparsers) -> None:
    p = subparsers.add_parser("deploys", help=HELP, description=(
        HELP + ". Read-only. " + NOTE + ". Decrypts the API key with sops on this machine and sends it on "
        "stdin to a container on the metrics network, which calls doco-cd."))
    p.add_argument("target", nargs="?", metavar="stack|run-id",
                   help="only runs that touched this Stack (as doco-cd names it), or one run in detail")
    p.add_argument("--status", choices=STATUSES, help="only runs with this status")
    p.add_argument("--limit", type=int, metavar="N", help=f"show at most N runs (default {DEFAULT_LIMIT})")
    output.add_json_flag(p)
    p.set_defaults(func=run)


def summary(r: dict) -> dict:
    return {
        "job_id": r.get("job_id"),
        "trigger": r.get("trigger"),
        "status": r.get("status"),
        "repository": r.get("repository"),
        "revision": r.get("revision"),
        "stacks": [d.get("stack") for d in r.get("deployments") or [] if d.get("stack")],
        "message": r.get("message"),
        "created_at": r.get("created_at"),
        "started_at": r.get("started_at"),
        "finished_at": r.get("finished_at"),
    }


def when(timestamp: str | None) -> str | None:
    """doco-cd's timestamp without fractional seconds; it keeps its own UTC offset."""
    return re.sub(r"\.\d+", "", timestamp) if timestamp else None


def stacks_cell(stacks: list[str]) -> str:
    return ", ".join(stacks) if len(stacks) <= WIDE else f"{len(stacks)} Stacks"


def run(args, ctx) -> int:
    run_id = args.target if args.target and RUN_ID.match(args.target) else None
    stack = None if run_id else args.target
    if run_id and (args.status or args.limit is not None):
        raise UsageError("--status and --limit apply to the list, not to one run")
    limit = DEFAULT_LIMIT if args.limit is None else args.limit
    if limit <= 0:
        raise UsageError("--limit must be a positive number")

    via = doco.transport(ctx.runner, ctx.declared, [])
    key = doco.api_key(ctx.repo_root, config.stack_root_name(ctx.env))
    if run_id:
        return show(ctx, args, via, key, run_id)

    query = f"limit={FETCH}" + (f"&status={args.status}" if args.status else "")
    runs = [summary(r) for r in doco.get(ctx.runner, via, f"/v1/api/runs?{query}", key).get("content") or []]
    if stack:
        runs = [r for r in runs if stack in r["stacks"]]
    runs = runs[:limit]
    if args.json:
        output.print_json({"runs": runs}, ctx.stdout)
        return 0
    if not runs:
        undeclared = f" ({stack} is not a Stack in this checkout.)" if stack and stack not in ctx.declared.stacks else ""
        ctx.stdout.write(f"No runs{' for ' + stack if stack else ''}"
                         f"{' with status ' + args.status if args.status else ''}.{undeclared} {NOTE}.\n")
        return 0
    output.table(["CREATED", "RUN", "TRIGGER", "STATUS", "STACKS", "MESSAGE"], [
        [when(r["created_at"]), r["job_id"], r["trigger"], r["status"], stacks_cell(r["stacks"]),
         " ".join((r["message"] or "").split()) or None] for r in runs], ctx.stdout)
    return 0


def show(ctx, args, via: str, key: str, run_id: str) -> int:
    try:
        r = summary(doco.get(ctx.runner, via, f"/v1/api/run/{quote(run_id, safe='')}", key).get("content") or {})
    except doco.NotFound as exc:
        raise doco.NotFound(f"{exc}. {NOTE}") from None
    if args.json:
        output.print_json(r, ctx.stdout)
        return 0
    rows = [
        ("Run", r["job_id"]), ("Trigger", r["trigger"]), ("Status", r["status"]),
        ("Source", f"{r['repository']} @ {r['revision']}" if r["repository"] else r["revision"]),
        ("Created", when(r["created_at"])), ("Started", when(r["started_at"])),
        ("Finished", when(r["finished_at"])), ("Stacks", ", ".join(r["stacks"]) or None),
    ]
    for label, value in rows:
        ctx.stdout.write(f"{label + ':':<10}{'-' if value is None else value}\n")
    ctx.stdout.write("Message:\n")
    for line in (r["message"] or "-").splitlines():
        ctx.stdout.write(f"  {line}\n")
    return 0
