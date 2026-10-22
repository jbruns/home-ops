"""hops restart <target…>: docker restart for each Service's container."""
from hops import lifecycle

HELP = "restart Services' containers (docker restart)"


def register(subparsers) -> None:
    p = subparsers.add_parser("restart", help=HELP, description=HELP + ". Runtime-reversible: prints the "
                              "containers, then restarts them.")
    lifecycle.add_targets(p)
    p.set_defaults(func=run)


def run(args, ctx) -> int:
    lifecycle.act(ctx, "restart", "Restarted", lifecycle.containers(ctx, ctx.declared.resolve(args.targets)))
    return 0
