"""hops strays [--apply [--volumes]]: list, or remove, what no Stack declares or uses."""
import json

from hops import config, docker, origin_main, output, strays
from hops.errors import ConfigError, UsageError

HELP = "Stray containers, images, volumes and networks; --apply removes them"
DESCRIPTION = (HELP + ". Lists by default (against this checkout) and changes nothing. --apply fetches "
               "origin/main, classifies against it, and removes stopped Stray containers, then Stray images and "
               "networks; volumes, the only Strays that can hold data, only with --apply --volumes. Anything "
               "whose ownership is unknown is listed but never removed.")


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


def remove_image(runner, image: dict) -> str | None:
    """Remove an image by its ID, without force; None on success, else why not.

    Docker refuses an ID with several tags. Then each tag is untagged, but only once every tag
    still resolves to this ID; anything that moved since the list was read is left alone.
    """
    result = runner.docker("image", "rm", image["id"], check=False)
    if result.returncode == 0:
        return None
    if "referenced in multiple repositories" not in result.stderr or not image["refs"]:
        return result.stderr.strip()
    for ref in image["refs"]:
        now = runner.docker("image", "inspect", "--format", "{{.Id}}", ref, check=False)
        if now.returncode != 0 or now.stdout.strip() != image["id"]:
            return f"{ref} changed since it was listed ({now.stdout.strip() or now.stderr.strip()}); skipped"
    for ref in image["refs"]:
        untag = runner.docker("image", "rm", ref, check=False)
        if untag.returncode != 0:
            return untag.stderr.strip()
    gone = runner.docker("image", "inspect", "--format", "{{.Id}}", image["id"], check=False)
    if gone.returncode == 0:
        return "still present after removing its tags (another reference holds it)"
    return None if "no such image" in gone.stderr.lower() else gone.stderr.strip()


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
        ("images", "Stray images:", ["IMAGE", "ID", "SIZE", "CREATED"],
         lambda i: [image_label(i), short(i["id"]), i["size"], i["created"]]),
        ("networks", "Stray networks:", ["NETWORK", "COMPOSE PROJECT"], lambda n: [n["network"], n["stack"]]),
        ("volumes", "Stray volumes (they can hold data):", ["VOLUME", "COMPOSE PROJECT", "NOTE"],
         lambda v: [v["volume"], v["stack"], note(v)]),
    ]
    first = True
    out.write(f"Declared state: {source}\n\n")
    if not any(report[k] for k in KINDS):
        out.write("No Strays.\n")
    for key, title, headers, row in sections:
        if report[key]:
            out.write(("" if first else "\n") + title + "\n")
            output.table(headers, [row(e) for e in report[key]], out)
            first = False


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
        source, said = f"{origin_main.REF}@{commit[:12]}", f"{origin_main.REF} ({commit[:12]}), fetched now"
    else:
        declared, source = ctx.declared, "checkout"
        said = f"this checkout ({ctx.repo_root}); --apply reads {origin_main.REF} instead"
    # A Stack's volume is never a Stray: refuse rather than guess which volume a variable names.
    unknown = strays.unresolved(declared.resource_names("volumes"))
    if args.volumes and unknown:
        raise ConfigError(f"cannot remove volumes: declared volume names use variables ({', '.join(unknown)})")
    report = {"declared_from": source, **read_host(ctx.runner, declared)}
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
        return 0 if ok else 1
    if not args.apply and any(report[k] for k in KINDS):
        ctx.stdout.write("\nNothing removed. hops strays --apply removes the Strays above not marked kept, except "
                         "volumes; add --volumes to remove those too.\n")
    elif args.apply and removable(report, "volumes") and not args.volumes:
        ctx.stdout.write("\nVolumes kept: add --volumes to remove them; they can hold data.\n")
    return 0 if ok else 1
