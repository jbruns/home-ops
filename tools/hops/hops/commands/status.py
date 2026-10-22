"""hops status [target…]: each declared Service against what runs on the Host."""
from hops import docker, output
from hops.declared import Service
from hops.strays import undeclared as no_declared_service

HELP = "declared vs running: state, health, restarts and digest drift for each Service"


def register(subparsers) -> None:
    p = subparsers.add_parser("status", help=HELP, description=HELP + ". Read-only.")
    p.add_argument("targets", nargs="*", metavar="target", help="Stacks or Services (default: all)")
    output.add_json_flag(p)
    p.set_defaults(func=run)


def service_row(service: Service, container: docker.Container | None) -> dict:
    declared_image = service.image.ref if service.image else service.spec.get("image")
    pinned = service.image.digest if service.image else None
    running = container.digest if container else None
    return {
        "stack": service.stack,
        "service": service.name,
        "tier": service.tier,
        "container": container.name if container else service.container,
        "state": container.status if container else "missing",
        "health": container.health if container else None,
        "restart_count": container.restart_count if container else None,
        "declared_image": declared_image,
        "running_image": container.image if container else None,
        # True means a Deploy did not apply: the container runs another digest than the repo pins.
        "digest_drift": (running != pinned) if (running and pinned) else None,
    }


def report(declared, services: list[Service], containers: list[docker.Container], undeclared: bool) -> dict:
    by_service: dict[str, list[docker.Container]] = {}
    strays = []
    for c in containers:
        owner = declared.by_compose_labels(c.labels)
        if owner:
            by_service.setdefault(owner.key, []).append(c)
        elif c.running and no_declared_service(declared, c):
            strays.append(c)
    return {
        "services": [service_row(s, docker.pick(by_service.get(s.key, []))) for s in services],
        "undeclared": [
            {"container": c.name, "image": c.image, "state": c.status,
             "stack": c.compose[0], "service": c.compose[1]}
            for c in sorted(strays, key=lambda c: c.name)
        ] if undeclared else [],
    }


def run(args, ctx) -> int:
    services = ctx.declared.resolve(args.targets) if args.targets else ctx.declared.services
    data = report(ctx.declared, services, docker.all_containers(ctx.runner), undeclared=not args.targets)
    if args.json:
        output.print_json(data, ctx.stdout)
        return 0
    output.table(
        ["SERVICE", "TIER", "STATE", "HEALTH", "RESTARTS", "DIGEST"],
        [[f"{r['stack']}/{r['service']}", r["tier"], r["state"], r["health"], r["restart_count"],
          {True: "DRIFT", False: "ok", None: None}[r["digest_drift"]]] for r in data["services"]],
        ctx.stdout,
    )
    drifted = [r for r in data["services"] if r["digest_drift"]]
    if drifted:
        ctx.stdout.write("\n")
    for r in drifted:
        ctx.stdout.write(f"{r['stack']}/{r['service']} runs {r['running_image']}, the repo pins "
                         f"{r['declared_image']} this checkout pins: a Deploy did not apply, or this checkout "
                         "is not what doco-cd deploys.\n")
    if data["undeclared"]:
        ctx.stdout.write("\nRunning containers no Stack declares:\n")
        output.table(["CONTAINER", "IMAGE", "COMPOSE PROJECT"],
                     [[u["container"], u["image"], u["stack"]] for u in data["undeclared"]], ctx.stdout)
    return 0
