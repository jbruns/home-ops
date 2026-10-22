"""Declared state: the Stacks and Services the repo's Stack root declares, and target name resolution.

Names resolve from the repo, never from running containers, so a typo fails before anything
touches the Host. Parsing of Compose files and labels is shared with
tools/declared-containers, which feeds ContainerDown the same list.
"""
import difflib
import functools
import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hops.errors import ConfigError, ResolveError

_DECLARED_CONTAINERS = Path(__file__).resolve().parents[2] / "declared-containers" / "declared_containers.py"
VARIABLE = re.compile(r"\$\{?[A-Za-z_]")
# Datastore (GLOSSARY) engines, by the last path segment of the image repository.
DATASTORE_IMAGES = {
    "mariadb": "mariadb", "mysql": "mariadb",
    "postgres": "postgres", "postgis": "postgres",
    "valkey": "valkey", "redis": "valkey",
}


# The labels Compose sets on each container; doco-cd's project is the Stack.
PROJECT = "com.docker.compose.project"
SERVICE = "com.docker.compose.service"


@functools.cache
def _dc():
    spec = importlib.util.spec_from_file_location("declared_containers", _DECLARED_CONTAINERS)
    if spec is None or not _DECLARED_CONTAINERS.is_file():
        raise ConfigError(f"{_DECLARED_CONTAINERS} is missing; install hops with uv tool install -e")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Image:
    ref: str  # as written in Compose: repository[:tag][@digest]
    repository: str
    tag: str | None
    digest: str | None

    @classmethod
    def parse(cls, ref: str) -> "Image":
        name, _, digest = ref.partition("@")
        repository, tag = name, None
        if ":" in name.rsplit("/", 1)[-1]:
            repository, tag = name.rsplit(":", 1)
        return cls(ref, repository, tag, digest or None)


@dataclass(frozen=True)
class Service:
    stack: str
    name: str
    file: Path  # the Compose file that declares it
    spec: dict = field(repr=False, compare=False)  # its Compose mapping, as written
    labels: dict[str, str] = field(repr=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.stack}/{self.name}"

    @property
    def tier(self) -> str | None:
        return self.labels.get("tier") or None

    @property
    def container_name(self) -> str | None:
        return self.spec.get("container_name")

    @property
    def container(self) -> str:
        """container_name, else Compose's default (a guess: the trailing number can grow)."""
        return self.container_name or f"{self.stack}-{self.name}-1"

    @property
    def image(self) -> Image | None:
        ref = self.spec.get("image")
        return Image.parse(str(ref)) if ref and not VARIABLE.search(str(ref)) else None

    @property
    def datastore(self) -> str | None:
        """The Datastore engine (mariadb, postgres, valkey) its image runs, or None."""
        if not self.image:
            return None
        return DATASTORE_IMAGES.get(self.image.repository.rsplit("/", 1)[-1])


class Declared:
    def __init__(self, root: Path, services: list[Service]):
        self.root = root
        self.services = services
        self.stacks: dict[str, list[Service]] = {}
        for s in services:
            self.stacks.setdefault(s.stack, []).append(s)

    @classmethod
    def load(cls, root: Path, stack_root: str = "tn") -> "Declared":
        """Every Service doco-cd deploys from <stack_root>/<stack>/compose.yaml and its includes."""
        dc = _dc()
        services = []
        for entry in sorted((root / stack_root).glob("*/compose.yaml")):
            stack = entry.parent.name  # doco-cd's Compose project name
            try:
                files = dc.compose_files(entry)
            except dc.GeneratorError as exc:
                raise ConfigError(str(exc)) from exc
            found = []
            for f in files:
                for name, spec in ((yaml.safe_load(f.read_text()) or {}).get("services") or {}).items():
                    spec = spec or {}
                    found.append(Service(stack, name, f, spec, dc.labels_of(spec)))
            services += sorted(found, key=lambda s: s.name)
        return cls(root, services)

    def get(self, stack: str, service: str) -> Service | None:
        return next((s for s in self.stacks.get(stack, []) if s.name == service), None)

    def by_compose_labels(self, labels: dict[str, str]) -> Service | None:
        """The Service a container belongs to, by the labels Compose puts on it."""
        return self.get(labels.get(PROJECT, ""), labels.get(SERVICE, ""))

    def resolve(self, names: list[str]) -> list[Service]:
        """Targets: stack/service, a bare Stack name (all its Services), or a unique bare Service name."""
        found: dict[str, Service] = {}
        for name in names:
            if "/" not in name and name in self.stacks:
                matches = self.stacks[name]
            else:
                matches = [self._one(name, allow_stack=False)]
            found.update((s.key, s) for s in matches)
        return list(found.values())

    def resolve_one(self, name: str) -> Service:
        """One Service: stack/service, a unique bare Service name, or a Stack that has only one."""
        return self._one(name, allow_stack=True)

    def _one(self, name: str, allow_stack: bool) -> Service:
        if "/" in name:
            stack, _, service = name.partition("/")
            if stack not in self.stacks:
                raise ResolveError(f"unknown Stack {stack!r}{self._suggest(stack, self.stacks)}")
            found = self.get(stack, service)
            if not found:
                have = ", ".join(s.name for s in self.stacks[stack])
                raise ResolveError(f"Stack {stack} has no Service {service!r} (it has {have})")
            return found
        matches = [s for s in self.services if s.name == name]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ResolveError(f"{name!r} is ambiguous: {', '.join(s.key for s in matches)}")
        if allow_stack and name in self.stacks:
            if len(self.stacks[name]) == 1:
                return self.stacks[name][0]
            raise ResolveError(f"Stack {name} has several Services; name one: "
                               + ", ".join(s.key for s in self.stacks[name]))
        known = {*self.stacks, *(s.name for s in self.services)}
        raise ResolveError(f"unknown Stack or Service {name!r}{self._suggest(name, known)}")

    @staticmethod
    def _suggest(name: str, known) -> str:
        close = difflib.get_close_matches(name, sorted(known), n=3)
        return f" (did you mean {' or '.join(close)}?)" if close else ""
