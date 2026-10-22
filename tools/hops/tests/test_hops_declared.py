"""The declared Stacks and Services, and how target names resolve to them."""
import pytest

from hops.declared import Declared
from hops.errors import ResolveError


@pytest.fixture
def declared(repo):
    return Declared.load(repo)


def keys(services):
    return [s.key for s in services]


def test_reads_every_stack_through_its_includes(declared):
    assert keys(declared.services) == [
        "doco-cd/doco-cd", "edge/traefik", "monitoring/alertmanager", "monitoring/prometheus",
        "nextcloud/exporter", "nextcloud/mariadb", "nextcloud/nextcloud", "nextcloud/valkey",
        "sonarr/exporter", "sonarr/sonarr",
    ]


def test_service_carries_tier_image_pin_and_container(declared):
    mariadb = declared.get("nextcloud", "mariadb")
    assert (mariadb.tier, mariadb.labels["startup-order"]) == ("critical", "1")
    assert (mariadb.image.repository, mariadb.image.tag, mariadb.image.digest) == ("mariadb", "11.8.9", "sha256:bbbb")
    assert mariadb.container == "mariadb"
    assert mariadb.file.as_posix().endswith("tn/nextcloud/mariadb/service.yaml")

    doco = declared.get("doco-cd", "doco-cd")
    assert doco.container_name is None
    assert doco.container == "doco-cd-doco-cd-1"

    unpinned = declared.get("sonarr", "exporter").image
    assert (unpinned.repository, unpinned.tag, unpinned.digest) == ("ghcr.io/onedr0p/exportarr", "v2.3.0", None)


def test_datastore_engine_comes_from_the_image(declared):
    assert declared.get("nextcloud", "mariadb").datastore == "mariadb"
    assert declared.get("nextcloud", "valkey").datastore == "valkey"
    assert declared.get("nextcloud", "exporter").datastore is None
    assert declared.get("nextcloud", "nextcloud").datastore is None


@pytest.mark.parametrize("names, expected", [
    (["nextcloud/exporter"], ["nextcloud/exporter"]),
    (["valkey"], ["nextcloud/valkey"]),
    (["edge"], ["edge/traefik"]),
    # A bare Stack name targets every Service in it, even when a Service shares its name.
    (["nextcloud"], ["nextcloud/exporter", "nextcloud/mariadb", "nextcloud/nextcloud", "nextcloud/valkey"]),
    (["nextcloud/nextcloud"], ["nextcloud/nextcloud"]),
    (["sonarr", "sonarr/sonarr", "traefik"], ["sonarr/exporter", "sonarr/sonarr", "edge/traefik"]),
])
def test_resolve_targets(declared, names, expected):
    assert keys(declared.resolve(names)) == expected


@pytest.mark.parametrize("names, message", [
    (["exporter"], r"'exporter' is ambiguous: nextcloud/exporter, sonarr/exporter"),
    (["sonar"], r"unknown Stack or Service 'sonar' \(did you mean sonarr\?\)"),
    (["nextcloud/redis"], r"Stack nextcloud has no Service 'redis' \(it has exporter, mariadb, nextcloud, valkey\)"),
    (["plex/plex"], r"unknown Stack 'plex'"),
])
def test_resolve_rejects_unknown_and_ambiguous_names(declared, names, message):
    with pytest.raises(ResolveError, match=message):
        declared.resolve(names)


def test_resolve_one_prefers_the_service_then_a_single_service_stack(declared):
    assert declared.resolve_one("nextcloud").key == "nextcloud/nextcloud"
    assert declared.resolve_one("edge").key == "edge/traefik"
    assert declared.resolve_one("sonarr/exporter").key == "sonarr/exporter"
    with pytest.raises(ResolveError, match=r"'doco' .*did you mean doco-cd"):
        declared.resolve_one("doco")
    with pytest.raises(ResolveError, match=r"'exporter' is ambiguous"):
        declared.resolve_one("exporter")


def test_resolve_one_needs_a_service_in_a_multi_service_stack(repo):
    (repo / "tn/nextcloud/nextcloud/service.yaml").write_text("services: {}\n")
    with pytest.raises(ResolveError, match=r"Stack nextcloud has several Services; name one: nextcloud/exporter, nextcloud/mariadb"):
        Declared.load(repo).resolve_one("nextcloud")


def test_finds_the_service_behind_compose_labels(declared):
    assert declared.by_compose_labels({"com.docker.compose.project": "nextcloud",
                                       "com.docker.compose.service": "valkey"}).key == "nextcloud/valkey"
    assert declared.by_compose_labels({"com.docker.compose.project": "old", "com.docker.compose.service": "x"}) is None
    assert declared.by_compose_labels({}) is None
