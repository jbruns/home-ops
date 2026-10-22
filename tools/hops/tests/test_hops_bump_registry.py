"""hops bump's registry client, against recorded Docker Hub, GHCR and lscr.io exchanges.

The expected digests are the ones Renovate pinned in tn/ for these tags (index digests).
"""
import pytest

from hops.errors import HopsError
from hops.registry import Registry, Response, locate
from hops_registry_fixtures import TOKEN, Replay

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")


@pytest.mark.parametrize("fixture, repository, tag, digest", [
    ("dockerhub", "traefik", "v3.7.15", "sha256:0f6150202bc1fed72a7bd2107460eaa510e5b8a905f3fdb5b84f2c8619c33368"),
    ("ghcr", "ghcr.io/home-operations/sonarr", "4.0.20.3012",
     "sha256:1f19eb5e0f421418c1a956bbe01310a0141423afe28bd9a4b1dcb8629ff2bce2"),
    ("lscr", "lscr.io/linuxserver/qbittorrent", "5.2.4",
     "sha256:1ea873647c43d566f251a7122aee330b184509c57ee3257ed19f62dac0f8f24b"),
])
def test_digest_is_the_index_digest_renovate_pins(fixture, repository, tag, digest):
    http = Replay(fixture)
    assert Registry(http).digest(repository, tag) == digest
    method, _, headers = http.requests[-1]
    assert method == "HEAD"
    assert all(t in headers["Accept"] for t in INDEX_TYPES)
    assert headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("fixture, repository, present", [
    ("dockerhub", "traefik", ["v3.7.15", "v3.7.16", "v2.11.7"]),
    ("ghcr", "ghcr.io/home-operations/sonarr", ["4.0.20.3012", "5.0.0.2742"]),
    ("lscr", "lscr.io/linuxserver/qbittorrent", ["5.2.4", "5.3.0"]),
])
def test_tags_follow_every_page(fixture, repository, present):
    http = Replay(fixture)
    tags = Registry(http).tags(repository)
    assert set(present) <= set(tags)
    pages = {url for method, url, headers in http.requests if "/tags/list" in url and "Authorization" in headers}
    assert pages == {e["url"] for e in http.exchanges if "/tags/list" in e["url"] and e["authorized"]}


def test_docker_hub_names_resolve_to_library_on_registry_1():
    assert locate("traefik") == ("registry-1.docker.io", "library/traefik")
    assert locate("docker.io/prom/prometheus") == ("registry-1.docker.io", "prom/prometheus")
    assert locate("ghcr.io/kimdre/doco-cd") == ("ghcr.io", "kimdre/doco-cd")
    assert locate("quay.io/prometheus/node-exporter") == ("quay.io", "prometheus/node-exporter")


def test_falls_back_to_hashing_the_manifest_when_head_has_no_digest_header():
    body = b'{"schemaVersion":2}'

    def http(method, url, headers):
        return Response(200, {}, b"" if method == "HEAD" else body)

    assert Registry(http).digest("registry.example/app", "1.0") == (
        "sha256:bafebd36189ad3688b7b3915ea55d461e0bfcfbdde11e54b0a123999fb6be50f")


def test_a_missing_tag_is_an_error():
    def http(method, url, headers):
        return Response(404, {}, b"")

    with pytest.raises(HopsError, match="ghcr.io/x/y:9.9: no such manifest"):
        Registry(http).digest("ghcr.io/x/y", "9.9")
