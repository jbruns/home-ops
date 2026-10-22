"""hops start <target…>: docker start for each Service's existing container."""
from hops import lifecycle

HELP = "start Services' stopped containers (docker start)"


def register(subparsers) -> None:
    p = subparsers.add_parser("start", help=HELP, description=HELP + ". Runtime-reversible: prints the "
                              "containers, then starts them. A silence from hops stop stays until it "
                              "expires (see hops alerts).")
    p.add_argument("targets", nargs="+", metavar="target", help="stack/service, a unique Service name, or a Stack")
    p.set_defaults(func=run)


def run(args, ctx) -> int:
    lifecycle.act(ctx, "start", "Started", lifecycle.containers(ctx, ctx.declared.resolve(args.targets)))
    return 0
