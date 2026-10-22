"""hops recreate <target…>: doco-cd force-recreates each Service (hops never runs docker compose)."""
from hops import config, doco, lifecycle
from hops.errors import UsageError

HELP = "force-recreate Services through doco-cd's REST API"


def register(subparsers) -> None:
    p = subparsers.add_parser("recreate", help=HELP, description=(
        HELP + ", e.g. after ContainerLeftoverName. Runtime-reversible: doco-cd recreates the Service "
        "from the Stack it last deployed. Decrypts the API key with sops on this machine and sends it "
        "on stdin to the prometheus container, which calls doco-cd."))
    lifecycle.add_targets(p)
    p.set_defaults(func=run)


def run(args, ctx) -> int:
    services = ctx.declared.resolve(args.targets)
    if any(s.key == doco.DOCO_CD for s in services):
        raise UsageError(f"{doco.DOCO_CD} cannot recreate itself; it replaces itself on its own Deploy")
    key = doco.api_key(ctx.repo_root, config.stack_root_name(ctx.env))
    for s in services:
        ctx.stdout.write(f"Recreating {s.key} through doco-cd...\n")
        ctx.stdout.flush()
        doco.recreate(ctx.runner, ctx.declared, s, key)
        ctx.stdout.write(f"Recreated {s.key}.\n")
    return 0
