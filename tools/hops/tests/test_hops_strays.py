"""hops strays: classify Stray containers, images, volumes and networks; remove them with --apply."""
import json

import pytest
from hops_fakes import container, write

# A Stack that names its volumes and networks: an external volume by name, one Compose names
# vault_cache, the shared proxy network by name, and one Compose names vault_backend.
VAULT = {
    "tn/vault/compose.yaml": """
        services:
          vaultwarden:
            image: vaultwarden/server:1.37.4
            container_name: vaultwarden
            labels: {tier: warning}
        volumes:
          data:
            name: home_vaultwarden_data
            external: true
          cache: {}
        networks:
          proxy:
            name: proxy
            external: true
          backend:
    """,
}


def img(n: int) -> str:
    return "sha256:" + str(n) * 64


def on_host(c: dict, *, image_id: str, labels: dict | None = None, volumes=(), networks=()) -> dict:
    c["Image"] = image_id
    c["Config"]["Labels"].update(labels or {})
    c["Mounts"] = [{"Type": "volume", "Name": v, "Destination": f"/{v}"} for v in volumes]
    c["NetworkSettings"] = {"Networks": {n: {} for n in networks}}
    return c


SKIP = {"startup-order": "skip"}
CONTAINERS = [
    on_host(container("traefik", project="edge", service="traefik"), image_id=img(1), networks=["proxy"]),
    # A stopped declared Service: its image is in use, its volume unmounted but still its Stack's.
    on_host(container("mariadb", project="nextcloud", service="mariadb", status="exited"), image_id=img(2)),
    # Running Strays are kept, with what they use: Portainer still deploys Stacks tn/ lacks.
    on_host(container("adhoc", image="adhoc:1"), image_id=img(8)),
    on_host(container("old-app", project="retired", service="app", status="exited", image="old:1"),
            image_id=img(3), volumes=["retired_data"], networks=["retired_default"]),
    # Deliberate docker runs are flagged; like any Stray, the running one is kept.
    on_host(container("spike", image="beyla:2"), image_id=img(4), labels=SKIP,
            volumes=["a" * 64], networks=["spike-net"]),
    on_host(container("helper", image="alpine:3", status="exited"), image_id=img(3), labels=SKIP),
    # A TrueNAS App.
    on_host(container("ix-foo-web-1", project="ix-foo", service="web"), image_id=img(5)),
]

IMAGES = [
    {"ID": img(1), "Repository": "traefik", "Tag": "v3.7.15", "Size": "200MB", "CreatedAt": "2026-10-01"},
    {"ID": img(2), "Repository": "mariadb", "Tag": "<none>", "Size": "400MB", "CreatedAt": "2026-10-02"},
    {"ID": img(3), "Repository": "alpine", "Tag": "3", "Size": "8MB", "CreatedAt": "2026-10-03"},
    {"ID": img(4), "Repository": "beyla", "Tag": "2", "Size": "50MB", "CreatedAt": "2026-10-04"},
    {"ID": img(5), "Repository": "ix-foo", "Tag": "1", "Size": "1MB", "CreatedAt": "2026-10-05"},
    {"ID": img(6), "Repository": "old", "Tag": "<none>", "Size": "90MB", "CreatedAt": "2026-09-01"},
    {"ID": img(7), "Repository": "foo", "Tag": "1", "Size": "10MB", "CreatedAt": "2026-09-02"},
    {"ID": img(7), "Repository": "foo", "Tag": "latest", "Size": "10MB", "CreatedAt": "2026-09-02"},
]

VOLUMES = [
    {"Name": "nextcloud_db", "Driver": "local", "Labels": "com.docker.compose.project=nextcloud,"
                                                          "com.docker.compose.volume=db"},
    {"Name": "home_vaultwarden_data", "Driver": "local", "Labels": ""},
    {"Name": "vault_cache", "Driver": "local", "Labels": ""},
    {"Name": "retired_data", "Driver": "local", "Labels": "com.docker.compose.project=retired"},
    {"Name": "a" * 64, "Driver": "local", "Labels": "com.docker.volume.anonymous="},
    {"Name": "b" * 64, "Driver": "local", "Labels": "com.docker.volume.anonymous="},
    {"Name": "ix-foo_data", "Driver": "local", "Labels": "com.docker.compose.project=ix-foo"},
]

NETWORKS = [
    {"ID": "n-bridge", "Name": "bridge", "Driver": "bridge", "Labels": ""},
    {"ID": "n-host", "Name": "host", "Driver": "host", "Labels": ""},
    {"ID": "n-none", "Name": "none", "Driver": "null", "Labels": ""},
    {"ID": "n-proxy", "Name": "proxy", "Driver": "bridge", "Labels": "com.docker.compose.project=edge"},
    {"ID": "n-backend", "Name": "vault_backend", "Driver": "bridge", "Labels": ""},
    {"ID": "n-retired", "Name": "retired_default", "Driver": "bridge",
     "Labels": "com.docker.compose.network=default,com.docker.compose.project=retired"},
    {"ID": "n-manual", "Name": "manual-net", "Driver": "bridge", "Labels": ""},
    {"ID": "n-spike", "Name": "spike-net", "Driver": "bridge", "Labels": ""},
    {"ID": "n-ix", "Name": "ix-foo_default", "Driver": "bridge", "Labels": "com.docker.compose.project=ix-foo"},
]

READS = [
    ["ps", "-aq", "--no-trunc"],
    ["inspect", *(c["Id"] for c in CONTAINERS)],
    ["image", "ls", "--no-trunc", "--format", "json"],
    ["volume", "ls", "--format", "json"],
    ["network", "ls", "--no-trunc", "--format", "json"],
]


def lines(rows: list[dict]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


@pytest.fixture
def host(repo, fake_host):
    write(repo, VAULT)
    fake_host.respond("ps", stdout="".join(c["Id"] + "\n" for c in CONTAINERS))
    fake_host.respond("inspect", stdout=json.dumps(CONTAINERS))
    fake_host.respond("image", "ls", stdout=lines(IMAGES))
    fake_host.respond("volume", "ls", stdout=lines(VOLUMES))
    fake_host.respond("network", "ls", stdout=lines(NETWORKS))
    for removal in (("rm",), ("image", "rm"), ("network", "rm"), ("volume", "rm")):
        fake_host.respond(*removal)
    return fake_host


def cid(name: str) -> str:
    return next(c["Id"] for c in CONTAINERS if c["Name"] == "/" + name)


def test_strays_json_classifies_each_kind(hops, host):
    outcome = hops("strays", "--json")
    assert outcome.code == 0, outcome.err
    report = json.loads(outcome.out)

    assert report["containers"] == [
        {"container": "adhoc", "id": cid("adhoc"), "image": "adhoc:1", "state": "running",
         "stack": None, "service": None, "deliberate": False, "kept": "running; stop it first to remove it",
         "removed": None},
        {"container": "helper", "id": cid("helper"), "image": "alpine:3", "state": "exited",
         "stack": None, "service": None, "deliberate": True, "kept": None, "removed": None},
        {"container": "old-app", "id": cid("old-app"), "image": "old:1", "state": "exited",
         "stack": "retired", "service": "app", "deliberate": False, "kept": None, "removed": None},
        {"container": "spike", "id": cid("spike"), "image": "beyla:2", "state": "running",
         "stack": None, "service": None, "deliberate": True,
         "kept": "running; stop it first to remove it", "removed": None},
    ]
    # alpine is used only by Strays that --apply removes; beyla by the kept spike; mariadb by a
    # stopped declared Service.
    assert report["images"] == [
        {"id": img(3), "repository": "alpine", "refs": ["alpine:3"], "size": "8MB", "created": "2026-10-03", "removed": None},
        {"id": img(6), "repository": "old", "refs": [], "size": "90MB", "created": "2026-09-01", "removed": None},
        {"id": img(7), "repository": "foo", "refs": ["foo:1", "foo:latest"], "size": "10MB", "created": "2026-09-02", "removed": None},
    ]
    # The stopped mariadb's unmounted volume is its Stack's, so never a Stray; likewise volumes a
    # Stack names, and the anonymous volume the kept spike mounts.
    assert report["volumes"] == [
        {"volume": "b" * 64, "stack": None, "anonymous": True, "removed": None},
        {"volume": "retired_data", "stack": "retired", "anonymous": False, "removed": None},
    ]
    assert report["networks"] == [
        {"network": "manual-net", "id": "n-manual", "stack": None, "removed": None},
        {"network": "retired_default", "id": "n-retired", "stack": "retired", "removed": None},
    ]


def test_default_run_only_reads(hops, host):
    assert hops("strays").code == 0
    assert host.docker_calls == READS


def test_table_lists_strays_and_says_nothing_was_removed(hops, host):
    out = hops("strays").out
    assert "adhoc" in out and "old-app" in out and "manual-net" in out and "retired_data" in out
    assert "deliberate docker run (startup-order=skip)" in out
    assert "old (untagged)" in out
    assert "kept: running; stop it first" in out
    assert "Nothing removed" in out and "--volumes" in out
    for kept in ("traefik", "mariadb", "ix-foo", "vault_cache", "home_vaultwarden_data", "proxy", "bridge"):
        assert kept not in out, kept


def test_apply_removes_containers_then_images_then_networks_but_never_volumes(hops, host):
    outcome = hops("strays", "--apply")
    assert outcome.code == 0, outcome.err
    assert host.docker_calls[len(READS):] == [
        ["rm", "-f", cid("helper")],
        ["rm", "-f", cid("old-app")],
        ["image", "rm", "alpine:3"],
        ["image", "rm", img(6)],
        ["image", "rm", "foo:1", "foo:latest"],
        ["network", "rm", "n-manual"],
        ["network", "rm", "n-retired"],
    ]
    assert not any(c[:2] == ["volume", "rm"] for c in host.docker_calls)
    assert "kept" in outcome.out and "--volumes" in outcome.out


def test_apply_with_volumes_also_removes_volumes(hops, host):
    report = json.loads(hops("strays", "--apply", "--volumes", "--json").out)
    assert host.docker_calls[-2:] == [["volume", "rm", "b" * 64], ["volume", "rm", "retired_data"]]
    assert [c["removed"] for c in report["containers"]] == [None, True, True, None]
    assert all(v["removed"] for v in report["volumes"])


def test_volumes_without_apply_is_a_usage_error(hops, fake_host):
    outcome = hops("strays", "--volumes")
    assert outcome.code == 2
    assert "--volumes needs --apply" in outcome.err
    assert fake_host.calls == []


def test_apply_keeps_going_after_a_failed_removal_and_exits_1(hops, host):
    host.respond("rm", "-f", cid("old-app"), stderr="Error: permission denied", returncode=1)
    outcome = hops("strays", "--apply", "--json")
    assert outcome.code == 1
    assert "old-app" in outcome.err and "permission denied" in outcome.err
    report = json.loads(outcome.out)
    assert [c["removed"] for c in report["containers"]] == [None, True, False, None]
    assert ["network", "rm", "n-retired"] in host.docker_calls


def test_apply_volumes_refuses_when_a_declared_volume_name_uses_a_variable(hops, repo, fake_host):
    write(repo, {"tn/vault/compose.yaml": VAULT["tn/vault/compose.yaml"].replace(
        "name: home_vaultwarden_data", "name: ${PREFIX}_vaultwarden_data")})
    outcome = hops("strays", "--apply", "--volumes")
    assert outcome.code == 2
    assert "${PREFIX}_vaultwarden_data" in outcome.err
    assert fake_host.calls == []


def test_no_strays(hops, fake_host):
    fake_host.respond("ps", stdout="")
    for kind in ("image", "volume", "network"):
        fake_host.respond(kind, "ls", stdout="")
    outcome = hops("strays", "--apply")
    assert outcome.code == 0
    assert "No Strays" in outcome.out
    assert fake_host.docker_calls == [READS[0], *READS[2:]]


def test_status_does_not_list_truenas_apps_as_undeclared(hops, host):
    undeclared = json.loads(hops("status", "--json").out)["undeclared"]
    assert [u["container"] for u in undeclared] == ["adhoc", "spike"]


@pytest.mark.live
def test_live_list_never_calls_a_stacks_data_a_stray(capsys):
    from hops import cli

    ctx = cli.Context()
    assert cli.main(["strays", "--json"], ctx=ctx) == 0  # list only: never --apply against the Host
    report = json.loads(capsys.readouterr().out)
    declared_volumes = ctx.declared.resource_names("volumes")
    assert not [v for v in report["volumes"] if v["volume"] in declared_volumes or v["stack"] in ctx.declared.stacks]
    assert not [c for c in report["containers"] if c["stack"] in ctx.declared.stacks and c["service"]
                and ctx.declared.get(c["stack"], c["service"])]
