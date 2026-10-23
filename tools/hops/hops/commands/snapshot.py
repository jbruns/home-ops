"""hops snapshot take|list|release: Rollback Snapshots (GLOSSARY, ADR 0011) through TrueNAS."""
from hops import output, snapshots

HELP = "Rollback Snapshots: take one before a risky change, list them, release one once it is verified"


def register(subparsers) -> None:
    p = subparsers.add_parser("snapshot", help=HELP, description=(
        HELP + ". A Rollback Snapshot is a ZFS snapshot carrying homeops:issue and homeops:expires; one past its "
        "release date is a Stray (hops strays). Only snapshots carrying those properties are listed or released."))
    sub = p.add_subparsers(dest="snapshot_command", metavar="<command>", required=True)

    take = sub.add_parser("take", help="snapshot a dataset before a risky change", description=(
        f"Snapshot DATASET (default {snapshots.DOCKER_DATASET}, Docker's data-root with every volume) as "
        f"{snapshots.PREFIX}<issue>-<UTC time>, with homeops:issue and homeops:expires set at creation."))
    take.add_argument("--issue", required=True, metavar="N", help="the issue or PR number of the change it serves")
    take.add_argument("--dataset", default=snapshots.DOCKER_DATASET, help="the ZFS dataset to snapshot")
    when = take.add_mutually_exclusive_group()
    when.add_argument("--expires", metavar="YYYY-MM-DD", help="release date (UTC)")
    when.add_argument("--for", dest="duration", metavar="DURATION",
                      help=f"release after this many days or weeks, e.g. 3d or 2w (default {snapshots.DEFAULT_DAYS}d)")
    take.add_argument("--dry-run", action="store_true", help="check and print what would be taken; take nothing")
    output.add_json_flag(take)
    take.set_defaults(func=run_take)

    ls = sub.add_parser("list", help="every Rollback Snapshot, with its issue, release date and size",
                        description="List every snapshot carrying homeops:issue or homeops:expires. Read-only.")
    output.add_json_flag(ls)
    ls.set_defaults(func=run_list)

    rel = sub.add_parser("release", help="destroy a Rollback Snapshot", description=(
        "Destroy a Rollback Snapshot. Refuses any snapshot without its own homeops:issue or homeops:expires, "
        "so periodic-task and TrueNAS system snapshots are never touched."))
    rel.add_argument("name", help="<dataset>@<snapshot> as take and list print it, or <snapshot> when unique")
    rel.add_argument("--dry-run", action="store_true", help="check and print what would be released; release nothing")
    output.add_json_flag(rel)
    rel.set_defaults(func=run_release)


def table(records: list[snapshots.RollbackSnapshot], out) -> None:
    output.table(["SNAPSHOT", "ISSUE", "EXPIRES", "SIZE", "EXPIRED"],
                 [[r.name, r.issue, r.expires, output.size(r.size), "yes" if r.expired else "no"] for r in records], out)


def run_take(args, ctx) -> int:
    at = snapshots.now()
    expires = snapshots.expiry(args.expires, args.duration, at.date())
    plan = snapshots.plan_take(ctx.truenas, args.issue, args.dataset, expires, at)
    full = f"{plan['dataset']}@{plan['name']}"
    if args.dry_run:
        if args.json:
            output.print_json({"snapshot": None, "would_take": {"name": full, "dataset": plan["dataset"],
                                                                 "issue": args.issue, "expires": expires}}, ctx.stdout)
        else:
            ctx.stdout.write(f"Would take {full} (issue #{args.issue}, release by {expires}). Nothing taken.\n")
        return 0
    taken = snapshots.take(ctx.truenas, plan)
    if args.json:
        output.print_json({"snapshot": taken.to_json(), "would_take": None}, ctx.stdout)
    else:
        ctx.stdout.write(f"{taken.name}\nTook it for issue #{taken.issue}; release it by {taken.expires} with "
                         f"hops snapshot release {taken.name}\n")
    return 0


def run_list(args, ctx) -> int:
    records = snapshots.rollback_snapshots(ctx.truenas)
    if args.json:
        output.print_json({"snapshots": [r.to_json() for r in records]}, ctx.stdout)
    elif records:
        table(records, ctx.stdout)
    else:
        ctx.stdout.write("No Rollback Snapshots.\n")
    return 0


def run_release(args, ctx) -> int:
    target = snapshots.releasable(ctx.truenas, args.name)
    if not args.dry_run:
        snapshots.release(ctx.truenas, target)
    if args.json:
        output.print_json({"snapshot": target.to_json(), "released": not args.dry_run}, ctx.stdout)
    elif args.dry_run:
        ctx.stdout.write(f"Would release {target.name} (issue #{target.issue}). Nothing released.\n")
    else:
        ctx.stdout.write(f"Released {target.name} (issue #{target.issue}).\n")
    return 0
