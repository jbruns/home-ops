"""Strays (GLOSSARY): containers, images, volumes and networks on the Host that no Stack declares or uses.

Pure classification over what the Host reports; hops/commands/strays.py reads the Host and removes.
Data that belongs to a Stack is never a Stray: a volume is kept when its Compose project is a declared
Stack or a Stack names it, even while nothing mounts it.
"""
from hops.declared import PROJECT, VARIABLE, Declared
from hops.docker import Container

DEFAULT_NETWORKS = {"bridge", "host", "none"}
ANONYMOUS = "com.docker.volume.anonymous"
# A deliberate `docker run` on the Host carries this label (README: startup-order=skip).
DELIBERATE_LABEL, DELIBERATE_VALUE = "startup-order", "skip"
# Docker removes these without force; a container in any other state may be doing work.
REMOVABLE_STATES = ("created", "exited", "dead")


def truenas_app(name: str, project: str | None) -> bool:
    """A TrueNAS App's resource: TrueNAS manages them as Compose projects named ix-<app>."""
    return name.startswith("ix-") or (project or "").startswith("ix-")


def undeclared(declared: Declared, c: Container) -> bool:
    """A container no declared Service accounts for, by its Compose labels or its declared
    container_name (a container deployed under a legacy Compose project), and not a TrueNAS App's."""
    return (declared.by_compose_labels(c.labels) is None and not truenas_app(c.name, c.compose[0])
            and c.name not in {s.container for s in declared.services})


def deliberate(c: Container) -> bool:
    return c.labels.get(DELIBERATE_LABEL) == DELIBERATE_VALUE


def parse_labels(text: str) -> dict[str, str]:
    """The Labels column of `docker volume/network ls --format json`: k=v pairs joined by commas."""
    return dict(item.partition("=")[::2] for item in (text or "").split(",") if item)


def unresolved(names: dict[str, str]) -> list[str]:
    """Declared names that interpolate a variable, so hops cannot tell which Host resource they mean."""
    return sorted(n for n in names if VARIABLE.search(n))


def classify(declared: Declared, containers: list[Container], images: list[dict], volumes: list[dict],
             networks: list[dict]) -> dict:
    """The Strays of each kind, as the JSON `hops strays` prints (README). Rows are `docker … ls` JSON.

    When ownership is unknown, a Stray is listed with the reason in `kept` and never removed. Images,
    volumes and networks used only by Strays that --apply removes are Strays too; what kept
    containers use is not.
    """
    stray_containers, kept = [], []
    for c in containers:
        if not undeclared(declared, c):
            kept.append(c)
            continue
        keep = None if c.status in REMOVABLE_STATES else f"{c.status}: only {', '.join(REMOVABLE_STATES[:-1])} " \
                                                          f"or {REMOVABLE_STATES[-1]} containers are removed"
        if keep:
            kept.append(c)
        stray_containers.append({
            "container": c.name, "id": c.id, "image": c.image, "state": c.status,
            "stack": c.compose[0], "service": c.compose[1], "deliberate": deliberate(c), "kept": keep,
            "removed": None,
        })

    used_images = {c.raw.get("Image") for c in kept}
    used_volumes = {m.get("Name") for c in kept for m in c.raw.get("Mounts") or [] if m.get("Type") == "volume"}
    used_networks = {n for c in kept for n in ((c.raw.get("NetworkSettings") or {}).get("Networks") or {})}
    # Compose projects a kept container runs under, legacy ones included: their volumes are theirs.
    kept_projects = {c.compose[0] for c in kept if c.compose[0]} | set(declared.stacks)

    by_id: dict[str, dict] = {}
    for row in images:
        if row["ID"] in used_images:
            continue
        repository = row.get("Repository") if row.get("Repository") not in ("", "<none>") else None
        entry = by_id.setdefault(row["ID"], {"id": row["ID"], "repository": repository, "refs": [],
                                             "size": row.get("Size"), "created": row.get("CreatedAt"),
                                             "kept": None, "removed": None})
        if repository and row.get("Tag") not in (None, "", "<none>"):
            entry["refs"].append(f"{row['Repository']}:{row['Tag']}")

    declared_volumes = declared.resource_names("volumes")
    stray_volumes = []
    for row in volumes:
        labels = parse_labels(row.get("Labels", ""))
        project = labels.get(PROJECT) or None
        name = row["Name"]
        if (project in kept_projects or name in declared_volumes or name in used_volumes
                or truenas_app(name, project)):
            continue
        anonymous = ANONYMOUS in labels
        if project:
            keep = f"Compose project {project} is no declared Stack: ownership unknown"
        elif not anonymous:
            keep = "named, unlabelled and undeclared: ownership unknown"
        else:
            keep = None  # an anonymous volume nothing references or claims
        stray_volumes.append({"volume": name, "stack": project, "anonymous": anonymous, "kept": keep,
                              "removed": None})

    declared_networks = declared.resource_names("networks")
    stray_networks = []
    for row in networks:
        project = parse_labels(row.get("Labels", "")).get(PROJECT) or None
        name = row["Name"]
        if (name in DEFAULT_NETWORKS or project in declared.stacks or name in declared_networks
                or name in used_networks or truenas_app(name, project)):
            continue
        stray_networks.append({"network": name, "id": row["ID"], "stack": project, "kept": None,
                               "removed": None})

    return {
        "containers": sorted(stray_containers, key=lambda r: r["container"]),
        "images": sorted(by_id.values(), key=lambda r: r["id"]),
        "volumes": sorted(stray_volumes, key=lambda r: r["volume"]),
        "networks": sorted(stray_networks, key=lambda r: r["network"]),
    }
