"""hops strays [--apply [--volumes]]: list, or remove, what no Stack declares or uses."""
import json

from hops import config, docker, origin_main, output, snapshots, strays
from hops.errors import ConfigError, HopsError, UsageError

HELP = "Stray containers, images, volumes, networks and expired Rollback Snapshots; --apply removes them"
DESCRIPTION = (HELP + ". Lists by default (against this checkout) and changes nothing. --apply fetches "
               "origin/main, classifies against it, and removes stopped Stray containers, then Stray images and "
               "networks; volumes, the only Strays that can hold data, only with --apply --volumes. Anything "
               "whose ownership is unknown is listed but never removed. Expired Rollback Snapshots are listed "
               "(through TrueNAS, when configured) and released only with hops snapshot release.")


def register(subparsers) -> None:
    p = subparsers.add_parser("strays", help=HELP, description=DESCRIPTION)
    p.add_argument("--apply", action="store_true", help="remove Stray containers, images and networks")
    p.add_argument("--volumes", action="store_true", help="with --apply, also remove Stray volumes")
    output.add_json_flag(p)
    p.set_defaults(func=run)


def json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


KINDS = ("containers", "images", "networks", "volumes")


def read_host(runner, declared) -> dict:
    return strays.classify(
        declared,
        docker.all_containers(runner),
        json_lines(runner.docker("image", "ls", "--no-trunc", "--format", "json").stdout),
        json_lines(runner.docker("volume", "ls", "--format", "json").stdout),
        json_lines(runner.docker("network", "ls", "--no-trunc", "--format", "json").stdout),
    )


def removable(report: dict, kind: str) -> list[dict]:
    return [e for e in report[kind] if not e["kept"]]


SNAPSHOT_KEPT = "released only with hops snapshot release, once its change is verified"


def expired_snapshots(ctx) -> tuple[list[dict] | None, str | None]:
    """Rollback Snapshots past their release date, as Strays; or None and why they weren't checked."""
    try:
        found = snapshots.rollback_snapshots(ctx.truenas)
    except ConfigError as exc:
        return None, str(exc)
    def row(r):
        fields = r.to_json()
        fields.pop("expired")  # true for every row here
        return {"snapshot": fields.pop("name"), **fields, "kept": SNAPSHOT_KEPT, "removed": None}

    return [row(r) for r in found if r.expired], None


class Kept(str):
    """A removal Docker refused for a reason that makes the item kept, not a failure."""


def remove_image(runner, image: dict) -> str | None:
    """Remove an image by its immutable ID, never by tag and without force; None on success.

    Docker refuses an ID with several tags. Those tags are mutable (a concurrent pull can move
    one), so the image is kept for a person to remove.
    """
    result = runner.docker("image", "rm", image["id"], check=False)
    if result.returncode == 0:
        return None
    if "referenced in multiple repositories" in result.stderr:
        return Kept(strays.multiple_tags(" ".join(image["refs"] + ["and tags added since it was listed"])))
    return result.stderr.strip()


def removals(report: dict, volumes: bool):
    """(kind, label, entry, remove) for each removal, in an order that frees what comes later.

    `remove(runner)` returns None on success, else the reason. Nothing is forced: Docker refuses
    anything that something started using since the list was read.
    """
    def docker_rm(*args):
        def remove(runner):
            result = runner.docker(*args, check=False)
            return None if result.returncode == 0 else result.stderr.strip()
        return remove

    for c in removable(report, "containers"):
        yield "container", c["container"], c, docker_rm("rm", c["id"])
    for i in removable(report, "images"):
        yield "image", image_label(i), i, lambda runner, i=i: remove_image(runner, i)
    for n in removable(report, "networks"):
        yield "network", n["network"], n, docker_rm("network", "rm", n["id"])
    if volumes:
        for v in removable(report, "volumes"):
            yield "volume", v["volume"], v, docker_rm("volume", "rm", v["volume"])


def apply(report: dict, ctx, volumes: bool, say) -> bool:
    ok = True
    for kind, label, entry, remove in removals(report, volumes):
        error = remove(ctx.runner)
        if isinstance(error, Kept):
            entry["kept"] = str(error)
            say(f"kept {kind} {label}: {error}\n")
            continue
        entry["removed"] = error is None
        if error is None:
            say(f"removed {kind} {label}\n")
        else:
            ok = False
            ctx.stderr.write(f"hops: error: could not remove {kind} {label}: {error}\n")
    return ok


def short(image_id: str) -> str:
    return image_id.removeprefix("sha256:")[:12]


def image_label(image: dict) -> str:
    return " ".join(image["refs"]) or f"{image['repository'] or ''} (untagged)".lstrip()


def print_report(report: dict, source: str, out) -> None:
    sections = [
        ("containers", "Stray containers:", ["CONTAINER", "IMAGE", "STATE", "COMPOSE PROJECT", "NOTE"],
         lambda c: [c["container"], c["image"], c["state"], c["stack"], note(c)]),
        ("images", "Stray images:", ["IMAGE", "ID", "SIZE", "CREATED", "NOTE"],
         lambda i: [image_label(i), short(i["id"]), i["size"], i["created"], note(i)]),
        ("networks", "Stray networks:", ["NETWORK", "COMPOSE PROJECT"], lambda n: [n["network"], n["stack"]]),
        ("volumes", "Stray volumes (they can hold data):", ["VOLUME", "COMPOSE PROJECT", "NOTE"],
         lambda v: [v["volume"], v["stack"], note(v)]),
    ]
    if report["snapshots"] is not None:
        sections.append(("snapshots", "Expired Rollback Snapshots:", ["SNAPSHOT", "ISSUE", "EXPIRES", "SIZE"],
                         lambda s: [s["snapshot"], s["issue"], s["expires"], output.size(s["size"])]))
    first = True
    out.write(f"Declared state: {source}\n\n")
    if not any(report[k] for k in KINDS) and not report["snapshots"]:
        out.write("No Strays" + (".\n" if report["snapshots"] is not None else " among Docker resources.\n"))
    for key, title, headers, row in sections:
        if report[key]:
            out.write(("" if first else "\n") + title + "\n")
            output.table(headers, [row(e) for e in report[key]], out)
            first = False
    if report["snapshots"] is None:
        out.write(f"\nRollback Snapshots not checked: {report['snapshots_unchecked']}\n")


def note(entry: dict) -> str | None:
    notes = []
    if entry.get("deliberate"):
        notes.append(f"deliberate docker run ({strays.DELIBERATE_LABEL}={strays.DELIBERATE_VALUE})")
    if entry.get("anonymous"):
        notes.append("anonymous")
    if entry["kept"]:
        notes.append("kept: " + entry["kept"])
    return "; ".join(notes) or None


def run(args, ctx) -> int:
    if args.volumes and not args.apply:
        raise UsageError("--volumes needs --apply")
    if args.apply:
        # Removal trusts only what doco-cd deploys: a missing or partial checkout must not make a
        # Stack's resources Strays.
        declared, commit = origin_main.load(ctx.repo_root, config.stack_root_name(ctx.env))
        source, said = f"{origin_main.REF}@{commit}", f"{origin_main.REF} at {commit}, fetched now"
    else:
        declared, source = ctx.declared, "checkout"
        said = f"this checkout ({ctx.repo_root}); --apply reads {origin_main.REF} instead"
    # A Stack's volume is never a Stray: refuse rather than guess which volume a variable names.
    unknown = strays.unresolved(declared.resource_names("volumes"))
    if args.volumes and unknown:
        raise ConfigError(f"cannot remove volumes: declared volume names use variables ({', '.join(unknown)})")
    report = {"declared_from": source, **read_host(ctx.runner, declared)}
    snapshots_ok = True
    try:
        report["snapshots"], report["snapshots_unchecked"] = expired_snapshots(ctx)
    except HopsError as exc:  # TrueNAS failed: report the Docker Strays all the same, and fail
        report["snapshots"], report["snapshots_unchecked"] = None, str(exc)
        ctx.stderr.write(f"hops: error: Rollback Snapshots not checked: {exc}\n")
        snapshots_ok = False
    say = (lambda _: None) if args.json else ctx.stdout.write
    if not args.json:
        print_report(report, said, ctx.stdout)
    ok = True
    if args.apply:
        if not args.json and any(removable(report, k) for k in KINDS):
            ctx.stdout.write("\n")
        ok = apply(report, ctx, args.volumes, say)
    if args.json:
        output.print_json(report, ctx.stdout)
        return 0 if ok and snapshots_ok else 1
    if not args.apply and any(report[k] for k in KINDS):
        ctx.stdout.write("\nNothing removed. hops strays --apply removes the Strays above not marked kept, except "
                         "volumes; add --volumes to remove those too.\n")
    elif args.apply and removable(report, "volumes") and not args.volumes:
        ctx.stdout.write("\nVolumes kept: add --volumes to remove them; they can hold data.\n")
    if report["snapshots"]:
        ctx.stdout.write("\nRelease each expired Rollback Snapshot with hops snapshot release <snapshot> once its "
                         "change is verified, or take a new one if it is still needed.\n")
    return 0 if ok and snapshots_ok else 1
