"""Shared by restart, start and stop: the containers a target names, printed before acting on them."""
from hops import docker
from hops.declared import Service


def containers(ctx, services: list[Service]) -> list[tuple[Service, str]]:
    """Each Service with the container name to pass to docker."""
    return [(s, docker.container_ref(ctx.runner, s)) for s in services]


def act(ctx, verb: str, done: str, found: list[tuple[Service, str]]) -> None:
    """Print the containers, then run `docker <verb>` on all of them in one call."""
    for service, name in found:
        ctx.stdout.write(f"{service.key} ({name})\n")
    ctx.runner.docker(verb, *(name for _, name in found))
    ctx.stdout.write(f"{done} {len(found)} container{'s' if len(found) != 1 else ''}.\n")
