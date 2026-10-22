"""hops logs <svc>: `docker logs` for a Service's container."""
from hops import docker, output
from hops.errors import UsageError

HELP = "a Service's container logs (docker logs), with --since, --tail and -f passed through"


def register(subparsers) -> None:
    p = subparsers.add_parser("logs", help=HELP, description=HELP + ". Read-only.")
    p.add_argument("service", help="a Service: stack/service, or a unique Service name")
    p.add_argument("--since", help="e.g. 10m, 2h, or a timestamp (docker logs --since)")
    p.add_argument("--tail", help="lines from the end (docker logs --tail)")
    p.add_argument("-f", "--follow", action="store_true", help="keep streaming (uses ssh -t); not with --json")
    output.add_json_flag(p)
    p.set_defaults(func=run)


def parse_lines(text: str, stream: str) -> list[dict]:
    lines = []
    for line in text.splitlines():
        time, _, rest = line.partition(" ")
        lines.append({"time": time, "stream": stream, "text": rest})
    return lines


def run(args, ctx) -> int:
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
