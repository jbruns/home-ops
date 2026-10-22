"""hops stop <target…> --for <duration>: silence the Services' alerts, then docker stop.

A stopped declared Service makes the Host differ from main: ContainerDown fires, and nothing starts
it again until a reboot or the next Deploy of its Stack. So stop is Disruptive: --for is required
and becomes an Alertmanager silence, and a Critical Service also needs --force.
"""
import re

from hops import alertmanager, lifecycle
from hops.errors import UsageError

HELP = "silence Services' alerts for a duration, then stop their containers (docker stop)"
# Prometheus's duration syntax, which amtool --duration takes: units in this order, each at most once.
DURATION = re.compile(r"(?:(\d+)y)?(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?(?:(\d+)ms)?")


def register(subparsers) -> None:
    p = subparsers.add_parser("stop", help=HELP, description=(
        HELP + ". Disruptive: nothing restarts a stopped Service until a reboot, the next Deploy of its "
        "Stack, or hops start. --for creates an Alertmanager silence for the Service (alerts labelled with "
        "its stack and service, such as ContainerDown) that expires after the duration."))
    lifecycle.add_targets(p)
    p.add_argument("--for", dest="duration", metavar="DURATION",
                   help="required: how long to silence its alerts, e.g. 30m, 2h, 1d (amtool --duration)")
    p.add_argument("--force", action="store_true", help="allow stopping a Critical Service")
    p.set_defaults(func=run)


def valid_duration(text: str) -> bool:
    m = DURATION.fullmatch(text)
    return bool(m) and any(int(g) for g in m.groups() if g)


def run(args, ctx) -> int:
    if not args.duration:
        raise UsageError("stop needs --for <duration> (e.g. --for 30m): ContainerDown fires and nothing "
                         "restarts the Service on its own, so its alerts are silenced for that long")
    if not valid_duration(args.duration):
        raise UsageError(f"--for {args.duration!r} is not a duration like 30m, 2h or 1d12h")
    services = ctx.declared.resolve(args.targets)
    critical = [s.key for s in services if s.tier == "critical"]
    if critical and not args.force:
        raise UsageError(f"{', '.join(critical)} {'is' if len(critical) == 1 else 'are'} Critical Tier; "
                         "pass --force to stop anyway")
    found = lifecycle.containers(ctx, services)
    author = f"hops ({ctx.env.get('USER') or 'unknown'})"
    for s in services:
        result = alertmanager.amtool(
            ctx.runner, ctx.declared, "silence", "add", f'stack="{s.stack}"', f'service="{s.name}"',
            f"--duration={args.duration}", f"--author={author}",
            f"--comment=hops stop {s.key} --for {args.duration}")
        ctx.stdout.write(f"Silenced {s.key} for {args.duration} (silence {result.stdout.strip()}).\n")
    lifecycle.act(ctx, "stop", "Stopped", found)
    names = " ".join(args.targets)
    ctx.stdout.write(f"Nothing restarts {'it' if len(found) == 1 else 'them'} automatically until a reboot or "
                     f"the next Deploy of the Stack. Start again with: hops start {names}\n")
    return 0
