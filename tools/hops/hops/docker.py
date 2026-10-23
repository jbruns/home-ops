"""Reading containers on the Host: `docker inspect` output as Containers, and finding a Service's."""
import json
from dataclasses import dataclass, field

from hops.declared import PROJECT, SERVICE, Image, Service
from hops.errors import HopsError
from hops.runner import Runner


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    image: str  # the reference it was created from (Compose keeps tag@digest)
    status: str  # created, running, paused, restarting, removing, exited, dead
    health: str | None  # healthy, unhealthy, starting; None without a healthcheck
    restart_count: int
    exit_code: int
    oom_killed: bool
    started_at: str
    finished_at: str
    created: str
    labels: dict[str, str]
    memory_limit: int | None  # bytes; None when unlimited
    raw: dict = field(repr=False, compare=False)

    @classmethod
    def from_inspect(cls, data: dict) -> "Container":
        state = data.get("State") or {}
        cfg = data.get("Config") or {}
        return cls(
            id=data["Id"],
            name=data["Name"].lstrip("/"),
            image=cfg.get("Image") or "",
            status=state.get("Status") or "",
            health=(state.get("Health") or {}).get("Status"),
            restart_count=data.get("RestartCount") or 0,
            exit_code=state.get("ExitCode") or 0,
            oom_killed=bool(state.get("OOMKilled")),
            started_at=state.get("StartedAt") or "",
            finished_at=state.get("FinishedAt") or "",
            created=data.get("Created") or "",
            labels=cfg.get("Labels") or {},
            memory_limit=(data.get("HostConfig") or {}).get("Memory") or None,
            raw=data,
        )

    @property
    def digest(self) -> str | None:
        return Image.parse(self.image).digest

    def bind_source(self, destination: str) -> str | None:
        """The Host path bind-mounted at `destination` in the container, if any."""
        return next((m.get("Source") for m in self.raw.get("Mounts") or []
                     if m.get("Type") == "bind" and m.get("Destination") == destination), None)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def compose(self) -> tuple[str | None, str | None]:
        """(project, service) from the labels Compose sets; doco-cd's project is the Stack."""
        return self.labels.get(PROJECT), self.labels.get(SERVICE)


def inspect(runner: Runner, *refs: str) -> list[Container]:
    if not refs:
        return []
    return [Container.from_inspect(d) for d in json.loads(runner.docker("inspect", *refs).stdout)]


def all_containers(runner: Runner) -> list[Container]:
    """Every container on the Host, running or not."""
    ids = runner.docker("ps", "-aq", "--no-trunc").stdout.split()
    if not ids:
        return []
    result = runner.docker("inspect", *ids, check=False)
    if result.returncode != 0 and not _only_vanished(result.stderr, ids):
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise HopsError(f"docker inspect failed: {detail}")
    return [Container.from_inspect(d) for d in json.loads(result.stdout)]


def _only_vanished(stderr: str, ids: list[str]) -> bool:
    """Whether inspect failed only because containers that ps listed are gone: a Deploy can remove
    them in between. Docker then still prints the others and exits 1."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    prefix = "Error: No such object: "
    return bool(lines) and all(line.startswith(prefix) and line[len(prefix):] in ids for line in lines)


def pick(containers: list[Container]) -> Container | None:
    """The container that stands for a Service: a running one, else the newest."""
    return max(containers, key=lambda c: (c.running, c.created), default=None)


def lookup(runner: Runner, service: Service) -> Container | None:
    """The Service's container, or None when it has none: by container_name, else by its Compose labels."""
    if service.container_name:
        result = runner.docker("inspect", service.container_name, check=False)
        if result.returncode == 0:
            return Container.from_inspect(json.loads(result.stdout)[0])
        if "no such" not in result.stderr.lower():
            raise HopsError(f"docker inspect {service.container_name} failed: {result.stderr.strip()}")
        return None
    ids = runner.docker("ps", "-aq", "--no-trunc", "--filter", f"label={PROJECT}={service.stack}",
                        "--filter", f"label={SERVICE}={service.name}").stdout.split()
    return pick(inspect(runner, *ids))


def find(runner: Runner, service: Service) -> Container:
    """The Service's container: by container_name, else by its Compose labels."""
    found = lookup(runner, service)
    if found:
        return found
    raise HopsError(f"{service.key} has no container on the Host (expected {service.container}); "
                    "see hops status")


def container_ref(runner: Runner, service: Service) -> str:
    """A name to pass to docker for the Service: container_name without a Host call, else looked up."""
    return service.container_name or find(runner, service).name
