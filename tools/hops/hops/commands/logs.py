"""hops logs <svc>: `docker logs` for a Service's container, or with --loki its history from Loki."""
from datetime import datetime, timezone

from hops import docker, loki, output
from hops.errors import UsageError

HELP = ("a Service's container logs (docker logs), with --since, --tail and -f passed through; "
        "--loki reads history from Loki instead")
LOKI_DEFAULT_SINCE = "1h"
LOKI_DEFAULT_LIMIT = 1000


def register(subparsers) -> None:
    p = subparsers.add_parser("logs", help=HELP, description=HELP + ". Read-only.")
    p.add_argument("service", nargs="?",
                   help="a Service: stack/service, or a unique Service name; with --loki also a bare Stack")
    p.add_argument("--since", help="e.g. 10m, 2h, or a timestamp (docker logs --since); "
                   f"with --loki default {LOKI_DEFAULT_SINCE}")
    p.add_argument("--tail", help="lines from the end (docker logs --tail)")
    p.add_argument("-f", "--follow", action="store_true", help="keep streaming (uses ssh -t); not with --json")
    loki_opts = p.add_argument_group("Loki history (survives container recreation)")
    loki_opts.add_argument("--loki", action="store_true", help="read the logs Alloy shipped to Loki, oldest first")
    loki_opts.add_argument("--until", help="end of the range: a duration back from now or a timestamp (default now)")
    loki_opts.add_argument("--grep", metavar="RE", help="only lines matching this RE2 regex (LogQL |~)")
    loki_opts.add_argument("--limit", type=int, help=f"at most this many lines (default {LOKI_DEFAULT_LIMIT})")
    loki_opts.add_argument("--query", metavar="LOGQL", help="a LogQL log query instead of a target")
    output.add_json_flag(p)
    p.set_defaults(func=run)


def parse_lines(text: str, stream: str) -> list[dict]:
    lines = []
    for line in text.splitlines():
        time, _, rest = line.partition(" ")
        lines.append({"time": time, "stream": stream, "text": rest})
    return lines


def run(args, ctx) -> int:
    if args.loki:
        return run_loki(args, ctx)
    loki_only = [f for f in ("until", "grep", "limit", "query") if getattr(args, f) is not None]
    if loki_only:
        raise UsageError(f"--{loki_only[0]} needs --loki")
    if not args.service:
        raise UsageError("logs needs a Service")
    if args.json and args.follow:
        raise UsageError("logs --json cannot --follow; use --since or --tail")
    service = ctx.declared.resolve_one(args.service)
    name = docker.container_ref(ctx.runner, service)
    opts = [*(["--since", args.since] if args.since else []), *(["--tail", args.tail] if args.tail else [])]
    if not args.json:
        return ctx.runner.attach("logs", *opts, *(["--follow"] if args.follow else []), name, tty=args.follow)
    result = ctx.runner.docker("logs", *opts, "--timestamps", name)
    lines = parse_lines(result.stdout, "stdout") + parse_lines(result.stderr, "stderr")
    output.print_json({"stack": service.stack, "service": service.name, "container": name,
                       "lines": sorted(lines, key=lambda line: line["time"])}, ctx.stdout)
    return 0


def run_loki(args, ctx) -> int:
    if args.tail or args.follow:
        raise UsageError("--loki takes --limit, not --tail or --follow")
    if bool(args.service) == bool(args.query):
        raise UsageError("logs --loki takes a target or --query, not both" if args.query
                         else "logs --loki needs a target or --query")
    limit = LOKI_DEFAULT_LIMIT if args.limit is None else args.limit
    if limit < 1:
        raise UsageError("--limit must be at least 1")
    now = datetime.now(timezone.utc)
    start = loki.parse_time(args.since or LOKI_DEFAULT_SINCE, now)
    end = loki.parse_time(args.until, now) if args.until else loki.to_ns(now)
    if start >= end:
        raise UsageError("--since must be before --until")
    stack = service = container = None
    if args.query:
        query = args.query
        services = []
    else:
        # A bare Stack name is every Service in it, as for the multi-target commands.
        services = ctx.declared.resolve([args.service])
        query = loki.selector(services)
        stack = services[0].stack
        if len(services) == 1:
            service, container = services[0].name, services[0].container_name
    query = loki.with_grep(query, args.grep)
    fetch = loki.fetcher(ctx.runner, loki.transport_image(ctx.declared))
    found, truncated = loki.query_range(fetch, query, start, end, limit)
    lines = [{"time": loki.rfc3339(ns), "stream": labels.get("logstream"), "text": text, "labels": labels}
             for ns, labels, text in found]
    if args.json:
        output.print_json({"stack": stack, "service": service, "container": container, "query": query,
                           "truncated": truncated, "lines": lines}, ctx.stdout)
    else:
        several = len(services) > 1
        for line in lines:
            prefix = f"{line['labels'].get('service', '?')} | " if several else ""
            ctx.stdout.write(f"{line['time']} {prefix}{line['text']}\n")
    if truncated:
        ctx.stderr.write(f"hops: stopped at --limit {limit}; Loki may have more lines: "
                         "raise --limit or narrow --since/--until\n")
    return 0
