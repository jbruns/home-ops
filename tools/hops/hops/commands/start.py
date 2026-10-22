"""hops start <target…>: docker start for each Service's existing container."""
from hops import lifecycle

HELP = "start Services' stopped containers (docker start)"


def register(subparsers) -> None:
    p = subparsers.add_parser("start", help=HELP, description=HELP + ". Runtime-reversible: prints the "
                              "containers, then starts them. Silences from hops stop stay until they "
                              "expire (see hops alerts).")
    lifecycle.add_targets(p)
    p.set_defaults(func=run)


def run(args, ctx) -> int:
    lifecycle.act(ctx, "start", "Started", lifecycle.containers(ctx, ctx.declared.resolve(args.targets)))
    return 0
