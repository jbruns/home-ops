"""hops stop <target…> --for <duration>: silence the Services' alerts, then docker stop.

A stopped declared Service makes the Host differ from main: ContainerDown fires, and nothing starts
it again until a reboot or the next Deploy of its Stack. So stop is Disruptive: --for is required
and becomes an Alertmanager silence, and a Critical Service also needs --force.
"""
import json
import re

from hops import alertmanager, config, lifecycle, silences
from hops.errors import UsageError

HELP = "silence Services' alerts for a duration, then stop their containers (docker stop)"
# Prometheus's duration syntax, which amtool --duration takes: units in this order, each at most once.
DURATION = re.compile(r"(?:(\d+)y)?(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?(?:(\d+)ms)?")


def register(subparsers) -> None:
    p = subparsers.add_parser("stop", help=HELP, description=(
        HELP + ". Disruptive: nothing restarts a stopped Service until a reboot, the next Deploy of its "
        "Stack, or hops start. --for creates Alertmanager silences that expire after the duration, one for "
        "each way an alert names the Service (stack and service, container, scrape target, Datastore), the "
        "same for Services in its Stack that depend on it, every Probe if they route through it, and its "
        "Stack's alerts that name no Service. Each is printed with its reason."))
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
    monitoring = silences.Monitoring.load(ctx.repo_root, config.stack_root_name(ctx.env))
    for silence in silences.plan(ctx.declared, found, monitoring):
        matcher_args = [f"{k}={json.dumps(v)}" for k, v in silence.matchers.items()]
        result = alertmanager.amtool(
            ctx.runner, ctx.declared, "silence", "add", *matcher_args,
            f"--duration={args.duration}", f"--author={author}",
            f"--comment=hops stop {silence.origin.key} --for {args.duration}: {silence.reason}")
        ctx.stdout.write(f"Silenced {' '.join(matcher_args)} for {args.duration} ({result.stdout.strip()}): "
                         f"{silence.reason}\n")
        ctx.stdout.flush()
    lifecycle.act(ctx, "stop", "Stopped", found)
    names = " ".join(args.targets)
    ctx.stdout.write(f"Nothing restarts {'it' if len(found) == 1 else 'them'} automatically until a reboot or "
                     f"the next Deploy of the Stack. Start again with: hops start {names}\n")
    return 0
