"""hops strays [--apply [--volumes]]: list, or remove, what no Stack declares or uses."""
import json

from hops import docker, output, strays
from hops.errors import ConfigError, UsageError

HELP = "Stray containers, images, volumes and networks; --apply removes them"
DESCRIPTION = (HELP + ". Lists by default and changes nothing. --apply removes stopped Stray containers, then "
               "Stray images and networks; volumes, the only Strays that can hold data, only with "
               "--apply --volumes.")


def register(subparsers) -> None:
    p = subparsers.add_parser("strays", help=HELP, description=DESCRIPTION)
    p.add_argument("--apply", action="store_true", help="remove Stray containers, images and networks")
    p.add_argument("--volumes", action="store_true", help="with --apply, also remove Stray volumes")
    output.add_json_flag(p)
    p.set_defaults(func=run)


def json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def read_host(ctx) -> dict:
    r = ctx.runner
    return strays.classify(
        ctx.declared,
        docker.all_containers(r),
        json_lines(r.docker("image", "ls", "--no-trunc", "--format", "json").stdout),
        json_lines(r.docker("volume", "ls", "--format", "json").stdout),
        json_lines(r.docker("network", "ls", "--no-trunc", "--format", "json").stdout),
    )


def removals(report: dict, volumes: bool):
    """(kind, label, entry, docker args) for each removal, in an order that frees what comes later."""
    for c in report["containers"]:
        if not c["kept"]:
            yield "container", c["container"], c, ["rm", "-f", c["id"]]
    for i in report["images"]:
        yield "image", image_label(i), i, ["image", "rm", *(i["refs"] or [i["id"]])]
    for n in report["networks"]:
        yield "network", n["network"], n, ["network", "rm", n["id"]]
    if volumes:
        for v in report["volumes"]:
            yield "volume", v["volume"], v, ["volume", "rm", v["volume"]]


def apply(report: dict, ctx, volumes: bool, say) -> bool:
    ok = True
    for kind, label, entry, args in removals(report, volumes):
        result = ctx.runner.docker(*args, check=False)
        entry["removed"] = result.returncode == 0
        if entry["removed"]:
            say(f"removed {kind} {label}\n")
        else:
            ok = False
            ctx.stderr.write(f"hops: error: could not remove {kind} {label}: {result.stderr.strip()}\n")
    return ok


def short(image_id: str) -> str:
    return image_id.removeprefix("sha256:")[:12]


def image_label(image: dict) -> str:
    return " ".join(image["refs"]) or f"{image['repository'] or ''} (untagged)".lstrip()


def print_report(report: dict, out) -> None:
    sections = [
        ("containers", "Stray containers:", ["CONTAINER", "IMAGE", "STATE", "COMPOSE PROJECT", "NOTE"],
         lambda c: [c["container"], c["image"], c["state"], c["stack"], note(c)]),
        ("images", "Stray images:", ["IMAGE", "ID", "SIZE", "CREATED"],
         lambda i: [image_label(i), short(i["id"]), i["size"], i["created"]]),
        ("networks", "Stray networks:", ["NETWORK", "COMPOSE PROJECT"], lambda n: [n["network"], n["stack"]]),
        ("volumes", "Stray volumes (they can hold data):", ["VOLUME", "COMPOSE PROJECT", "NOTE"],
         lambda v: [v["volume"], v["stack"], "anonymous" if v["anonymous"] else None]),
    ]
    first = True
    for key, title, headers, row in sections:
        if report[key]:
            out.write(("" if first else "\n") + title + "\n")
            output.table(headers, [row(e) for e in report[key]], out)
            first = False


def note(c: dict) -> str | None:
    notes = [f"deliberate docker run ({strays.DELIBERATE_LABEL}={strays.DELIBERATE_VALUE})"] if c["deliberate"] else []
    if c["kept"]:
        notes.append("kept: " + c["kept"])
    return "; ".join(notes) or None


def run(args, ctx) -> int:
    if args.volumes and not args.apply:
        raise UsageError("--volumes needs --apply")
    # A Stack's volume is never a Stray: refuse rather than guess which volume a variable names.
    unknown = strays.unresolved(ctx.declared.resource_names("volumes"))
    if args.volumes and unknown:
        raise ConfigError(f"cannot remove volumes: declared volume names use variables ({', '.join(unknown)})")
    report = read_host(ctx)
    say = (lambda _: None) if args.json else ctx.stdout.write
    if not args.json:
        if any(report.values()):
            print_report(report, ctx.stdout)
        else:
            ctx.stdout.write("No Strays.\n")
    ok = True
    if args.apply:
        if not args.json and any(report.values()):
            ctx.stdout.write("\n")
        ok = apply(report, ctx, args.volumes, say)
    if args.json:
        output.print_json(report, ctx.stdout)
    elif any(report.values()):
        tail = []
        if not args.apply:
            tail.append("Nothing removed. hops strays --apply removes the containers, images and networks above "
                        "(except running containers); add --volumes to remove the volumes too.")
        elif report["volumes"] and not args.volumes:
            tail.append("Volumes kept: add --volumes to remove them; they can hold data.")
        for line in tail:
            ctx.stdout.write("\n" + line + "\n")
    return 0 if ok else 1
