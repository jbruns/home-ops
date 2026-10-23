"""hops volume move <old> <new> --issue <n>: the runtime half of a volume rename (ADR 0010).

The declared half is the PR changing the volume's `name:`. This creates <new>, renames every entry of
<old>/_data into it from one container that mounts Docker's volumes directory once, removes the
stopped containers that still reference <old> (the Deploy after merge recreates them under the new
name), and removes <old>. A Rollback Snapshot (ADR 0011) of the dataset holding the volumes is
required, and doco-cd must be stopped: on each poll it Deploys every Stack that differs from declared
state, a Stack stopped for the move included. If anything fails part-way it stops, reports both
volumes' state, and rolls nothing back.
"""
import re
from datetime import datetime, timezone

from hops import docker, doco, output, snapshots, strays, volumes
from hops.errors import ConfigError, HopsError, HostTimeout, UsageError

# Docker's FinishedAt: RFC 3339 in UTC, to the nanosecond.
FINISHED = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z")
HELP = "rename a volume: move its contents into a new volume, then remove the old one"


def register(subparsers) -> None:
    p = subparsers.add_parser("volume", help="volume operations (move)", description="Volume operations.")
    sub = p.add_subparsers(dest="volume_command", metavar="<command>", required=True)
    mv = sub.add_parser("move", help=HELP, description=(
        HELP + ". The runtime half of a rename (ADR 0010); the PR changing the volume's name: is the declared "
        "half. Refuses unless doco-cd and every container mounting OLD are stopped, NEW is missing or empty, "
        "and a Rollback Snapshot for --issue covers the dataset holding the volumes (or --take-snapshot takes one). "
        "Stopped containers that mount OLD are removed so OLD can be; the Deploy recreates them. "
        "Runbook: tools/hops/README.md#renaming-a-volume."))
    mv.add_argument("old", help="the volume to move from; removed afterwards")
    mv.add_argument("new", help="the volume to move into; created unless it exists and is empty")
    mv.add_argument("--issue", required=True, metavar="N", help="the issue or PR of the rename")
    mv.add_argument("--take-snapshot", action="store_true",
                    help="take the Rollback Snapshot now (released by default in "
                         f"{snapshots.DEFAULT_DAYS} days) instead of requiring one")
    mv.add_argument("--dry-run", action="store_true", help="check every precondition and change nothing")
    output.add_json_flag(mv)
    mv.set_defaults(func=run_move)


def refuse(message: str) -> None:
    raise UsageError(f"refusing to move: {message}")


def declared_service(declared, c):
    """The declared Service a container is, by its Compose labels or its container name; else None."""
    return declared.by_compose_labels(c.labels) or next((s for s in declared.services if s.container == c.name), None)


def describe(declared, c) -> dict:
    service = declared_service(declared, c)
    return {"container": c.name, "id": c.id, "state": c.status, "service": service.key if service else None,
            "deliberate": strays.deliberate(c)}


def deployer(ctx):
    """doco-cd's container, or None. On each poll doco-cd Deploys every Stack that differs from declared
    state, so it would start a Stack stopped for the move."""
    return docker.lookup(ctx.runner, ctx.declared.resolve_one(doco.DOCO_CD))


def check_deployer(ctx):
    """doco-cd's container, stopped (or None); refuses while it is active."""
    c = deployer(ctx)
    if c and c.status not in strays.REMOVABLE_STATES:
        refuse(f"doco-cd ({c.name}, {c.status}) would Deploy the stopped Stack on its next poll; "
               "pause it for the whole rename first with hops stop doco-cd --for <duration> --force")
    return c


def check_users(ctx, old: str, new: str) -> list:
    """The containers that reference <old>, all stopped and removable; refuses otherwise."""
    on_old = volumes.users(ctx.runner, old)
    running = [f"{c.name} ({c.status})" for c in on_old if c.status not in strays.REMOVABLE_STATES]
    if running:
        refuse(f"{', '.join(running)} still mount{'s' if len(running) == 1 else ''} {old}; "
               "stop the Stack first with hops stop <stack> --for <duration>")
    unknown = [c.name for c in on_old if not declared_service(ctx.declared, c) and not strays.deliberate(c)]
    if unknown:
        refuse(f"stopped container {', '.join(unknown)} mounts {old} but is no declared Service and not "
               "labelled startup-order=skip, so hops will not remove it; remove it yourself first")
    busy_new = [f"{c.name} ({c.status})" for c in volumes.users(ctx.runner, new)
                if c.status not in strays.REMOVABLE_STATES]
    if busy_new:
        refuse(f"{', '.join(busy_new)} mount{'s' if len(busy_new) == 1 else ''} {new}; stop it first")
    return on_old


def truenas(ctx):
    try:
        return ctx.truenas
    except ConfigError as exc:
        raise UsageError(f"refusing to move: a rename needs a Rollback Snapshot (ADR 0011), and {exc}") from None


def last_stop(containers) -> tuple[datetime, str] | None:
    """When the last of the containers stopped (to the microsecond, by the Host's clock), and which."""
    stops = []
    for c in containers:
        m = FINISHED.fullmatch(c.finished_at)
        if not m:
            continue
        at = datetime.strptime(m[1], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc, microsecond=int((m[2] or "").ljust(6, "0")[:6]))
        stops.append((at, c.name))
    return max(stops, default=None)


def stale(name: str, created: datetime | None, stop: tuple[datetime, str]) -> str | None:
    """Why the snapshot may hold data mid-write, or None when ZFS created it strictly after the stop.

    Both times are the Host's clock. ZFS keeps `creation` to the second, so a snapshot created in the
    second the last container stopped may be from before the stop, and does not count."""
    at, who = stop
    if created is None:
        return f"{name} has no readable creation time"
    if created > at:
        return None
    if created == at.replace(microsecond=0):
        return (f"{name} was created in the second {who} stopped ({at:%Y-%m-%d %H:%M:%S.%f}Z), and ZFS keeps "
                "creation to the second, so it may be from before the stop")
    return f"{name} was created at {created:%Y-%m-%d %H:%M:%S}Z, before {who} stopped ({at:%Y-%m-%d %H:%M:%S.%f}Z)"


def snapshot_plan(ctx, args, dataset: str, on_old):
    """The covering Rollback Snapshot (a record), or with --take-snapshot the plan for one (a dict).

    A covering snapshot was taken after every container on <old> stopped, so it holds data at rest."""
    client = truenas(ctx)
    if args.take_snapshot:
        at = snapshots.now()
        return snapshots.plan_take(client, args.issue, dataset, snapshots.expiry(None, None, at.date()), at)
    take = (f"take one now the Stack is stopped (hops snapshot take --issue {args.issue} --dataset {dataset}) "
            "or pass --take-snapshot")
    found = [s for s in snapshots.rollback_snapshots(client)
             if s.issue == args.issue and s.dataset == dataset and not s.expired]
    if not found:
        refuse(f"no unexpired Rollback Snapshot of {dataset} (which holds the volumes) for issue #{args.issue}; {take}")
    stop = last_stop(on_old)
    if not stop:
        return found[-1]
    created = snapshots.created(client, [s.name for s in found])
    reasons = {s.name: stale(s.name, created.get(s.name), stop) for s in found}
    fresh = sorted((s for s in found if not reasons[s.name]), key=lambda s: created[s.name])
    if not fresh:
        refuse(f"{'; '.join(reasons.values())}; so {'it' if len(found) == 1 else 'they'} may hold data mid-write; "
               f"{take}")
    return fresh[-1]


def run_of(c) -> tuple | None:
    return c and (c.id, c.started_at, c.finished_at)


def late_consumers(ctx, old: str, new: str, on_old, stopped_deployer) -> list[str]:
    """Containers active since the checks (a Deploy of the Stack, say): any on <old> that is not one of
    the stopped `on_old`, is no longer stopped, or has started or stopped since (it may have written
    and exited again), any running on <new>, and doco-cd's on the same terms as `stopped_deployer`."""
    known = {c.id: (c.started_at, c.finished_at) for c in on_old}
    late = []
    c = deployer(ctx)
    if c and c.status not in strays.REMOVABLE_STATES:
        late.append(f"{c.name} ({c.status}, doco-cd)")
    elif c and run_of(c) != run_of(stopped_deployer):
        late.append(f"{c.name} ({c.status}, doco-cd ran since the checks)")
    for c in volumes.users(ctx.runner, old):
        if c.id not in known or c.status not in strays.REMOVABLE_STATES:
            late.append(f"{c.name} ({c.status})")
        elif (c.started_at, c.finished_at) != known[c.id]:
            late.append(f"{c.name} ({c.status}, ran since the checks)")
    late += [f"{c.name} ({c.status})" for c in volumes.users(ctx.runner, new)
             if c.status not in strays.REMOVABLE_STATES]
    return late


def state(ctx, image: str, directory: str, old: str, new: str, probe=None) -> str:
    """Both volumes' state for a failure report: from the move's own report, else read again."""
    try:
        probe = probe or volumes.check(ctx.runner, image, directory, old, new)
    except HopsError as exc:
        return f"the state of {old} and {new} is unknown ({exc}); look before doing anything else"

    def count(v, name):
        return f"{name} is missing" if v is None else f"{name} holds {v['entries']} top-level entries"
    return f"{count(probe.old, old)}, {count(probe.new, new)}"


def run_move(args, ctx) -> int:
    old, new = args.old, args.new
    volumes.check_name(old)
    volumes.check_name(new)
    if old == new:
        raise UsageError("old and new are the same volume")
    snapshots.check_issue(args.issue)
    image = volumes.image(ctx.declared)
    warnings = [w for w in [volumes.convention(ctx.declared, new)] if w]
    for w in warnings:
        ctx.stderr.write(f"hops: warning: {w}\n")

    directory = volumes.volumes_dir(ctx.runner)
    old_volume = volumes.inspect(ctx.runner, old)
    if old_volume is None:
        refuse(f"there is no volume {old}")
    new_volume = volumes.inspect(ctx.runner, new)
    for volume in (old_volume, new_volume):
        problem = volume and volumes.local_problem(volume, directory)
        if problem:
            refuse(f"{volume['Name']}: {problem}")
    stopped_deployer = check_deployer(ctx)
    on_old = check_users(ctx, old, new)

    probe = volumes.check(ctx.runner, image, directory, old, new)
    if probe.old is None:
        refuse(f"{directory}/{old}/_data is not a directory")
    if probe.new is not None and probe.new["entries"]:
        refuse(f"{new} exists and is not empty ({probe.new['entries']} top-level entries)")
    if probe.old["dev"] != probe.dev or (probe.new is not None and probe.new["dev"] != probe.dev):
        refuse(f"the volumes are not all on the filesystem of {directory}, so rename(2) cannot move them")
    if probe.fstype != "zfs" or not probe.source:
        refuse(f"{directory} is on {probe.fstype or 'an unknown filesystem'}, not a ZFS dataset, "
               "so no Rollback Snapshot can cover it")
    dataset = probe.source
    snap = snapshot_plan(ctx, args, dataset, on_old)

    report = {"old": old, "new": new, "issue": args.issue, "dataset": dataset, "entries": probe.old["entries"],
              "snapshot": None, "would_take": None, "created_new": new_volume is None,
              "containers": [describe(ctx.declared, c) for c in on_old], "warnings": warnings, "dry_run": args.dry_run,
              "moved": False}
    if isinstance(snap, dict):
        report["would_take"] = snapshots.would_take(snap)
    else:
        report["snapshot"] = snap.to_json()
    if args.dry_run:
        return finish(ctx, args, report)

    if isinstance(snap, dict):
        snap = snapshots.take(ctx.truenas, snap)
        report["snapshot"], report["would_take"] = snap.to_json(), None
        ctx.stderr.write(f"Took Rollback Snapshot {snap.name} (release by {snap.expires}).\n")

    def stopped(step: str, detail: str, probe=None):
        raise HopsError(
            f"{step} failed: {detail}. Stopped there and rolled nothing back: "
            f"{state(ctx, image, directory, old, new, probe)}. Rollback Snapshot {snap.name} holds the data "
            f"from before the move: restore {old} from it (README, Risky changes), or finish the move by hand")

    if new_volume is None:
        try:
            ctx.runner.docker("volume", "create", new)
            created = volumes.inspect(ctx.runner, new)
        except HopsError as exc:
            stopped(f"docker volume create {new}", str(exc))
        problem = volumes.local_problem(created, directory) if created else "it does not exist"
        if problem:
            stopped(f"docker volume create {new}", problem)
    # Again just before the move, and after it before any cleanup: Docker cannot lock a volume, and a
    # Deploy landing now would start the Stack on it.
    try:
        late = late_consumers(ctx, old, new, on_old, stopped_deployer)
    except HopsError as exc:
        stopped("re-checking containers before the move", str(exc))
    if late:
        raise HopsError(
            f"{', '.join(late)} started after the checks passed (a Deploy of the Stack?), so nothing was moved: "
            f"{state(ctx, image, directory, old, new)}. Stop it, then run hops volume move again; Rollback "
            f"Snapshot {snap.name} is still there")
    try:
        moved = volumes.move(ctx.runner, image, directory, old, new)
    except HostTimeout as exc:
        stopped("the move", f"{exc}; its container (label {volumes.LABEL}={old}) may still be running on the "
                "Host, so wait for it to exit before trusting what follows")
    except HopsError as exc:
        stopped("the move", str(exc))
    try:
        late = late_consumers(ctx, old, new, on_old, stopped_deployer)
    except HopsError as exc:
        stopped("re-checking containers after the move", str(exc), moved)
    if late:
        stopped("the move", f"{', '.join(late)} started during the move, so files were renamed under it; stop it "
                f"now, before anything else{f' (the move itself reported {moved.error})' if moved.error else ''}",
                moved)
    if moved.error:
        stopped("the move", f"{moved.error} after {moved.moved} of {probe.old['entries']} entries", moved)
    if moved.old is None or moved.new is None or moved.old["entries"] or moved.new["entries"] != probe.old["entries"]:
        stopped("checking the move", f"expected {old} empty and {probe.old['entries']} entries in {new}", moved)
    removed = []

    def removing(exc, probe):
        left = [c.name for c in on_old if c.name not in removed]
        stopped(f"removing {old}", f"{exc} (containers removed: {', '.join(removed) or 'none'}; "
                f"still there: {', '.join(left) or 'none'})", probe)
    try:
        for c in on_old:
            ctx.runner.docker("rm", c.id)
            removed.append(c.name)
        # Read <old> once more right before removing it: anything written since the move would go with it.
        last = volumes.check(ctx.runner, image, directory, old, new)
    except HopsError as exc:
        removing(exc, moved)
    if last.old is None or last.old["entries"]:
        removing(f"{old} is not empty just before its removal, so hops kept it", last)
    try:
        ctx.runner.docker("volume", "rm", old)
    except HopsError as exc:
        removing(exc, last)
    report["moved"] = True
    return finish(ctx, args, report)


def finish(ctx, args, report: dict) -> int:
    if args.json:
        output.print_json(report, ctx.stdout)
        return 0
    old, new, n = report["old"], report["new"], report["entries"]
    snap = report["snapshot"] or report["would_take"]
    containers = ", ".join(f"{c['container']} ({c['state']})" for c in report["containers"]) or "none"
    if report["dry_run"]:
        ctx.stdout.write(
            f"Would move {n} top-level entries from {old} to {new} ({'creating it' if report['created_new'] else 'empty'}) "
            f"on {report['dataset']}, remove the stopped containers that reference {old} ({containers}), and "
            f"remove {old}.\nRollback Snapshot: {snap['name']}"
            f"{' (would take it)' if report['would_take'] else ''}. Nothing changed.\n")
        return 0
    stacks = sorted({c["service"].split("/")[0] for c in report["containers"] if c["service"]})
    ctx.stdout.write(
        f"Moved {n} top-level entries from {old} to {new}; removed containers: {containers}; removed {old}.\n"
        "Next:\n"
        f"  1. Merge the PR that renames {old} to {new}.\n"
        "  2. Start doco-cd: hops start doco-cd. Its Deploy of the merge recreates the Stack's containers.\n"
        f"  3. Verify: hops status {' '.join(stacks) or '<stack>'}, and the Stack works as before.\n"
        f"  4. Release the Rollback Snapshot: hops snapshot release {snap['name']}\n")
    return 0
