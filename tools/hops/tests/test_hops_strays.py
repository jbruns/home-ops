"""hops strays: classify Stray containers, images, volumes and networks; remove them with --apply.

When ownership is unknown, a resource is listed but never removed.
"""
import json
import subprocess
from datetime import datetime, timezone

import pytest
from hops_fakes import FakeSnapshots, container, git, rollback, snap, write

from hops import snapshots

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
    # A declared container_name under a legacy Compose project (deployed before the Stack moved to
    # tn/): it, what it mounts, and its project's volumes are kept.
    on_host(container("vaultwarden", project="home", service="vaultwarden", status="exited"), image_id=img(9),
            volumes=["home_extra"]),
    # Only created, exited and dead Strays are removed; running, paused and restarting ones are kept.
    on_host(container("adhoc", image="adhoc:1"), image_id=img(8)),
    on_host(container("frozen", image="adhoc:1", status="paused"), image_id=img(8)),
    on_host(container("old-app", project="retired", service="app", status="exited", image="old:1"),
            image_id=img(3), volumes=["retired_data"], networks=["retired_default"]),
    # Deliberate docker runs are flagged; otherwise they are like any Stray.
    on_host(container("spike", image="beyla:2"), image_id=img(4), labels=SKIP,
            volumes=["a" * 64], networks=["spike-net"]),
    on_host(container("helper", image="alpine:3", status="dead"), image_id=img(3), labels=SKIP),
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
    {"ID": img(8), "Repository": "adhoc", "Tag": "1", "Size": "1MB", "CreatedAt": "2026-09-03"},
    {"ID": img(9), "Repository": "vaultwarden/server", "Tag": "1.37.4", "Size": "1MB", "CreatedAt": "2026-09-04"},
]

ANON = "com.docker.volume.anonymous="
VOLUMES = [
    {"Name": "nextcloud_db", "Driver": "local", "Labels": "com.docker.compose.project=nextcloud,"
                                                          "com.docker.compose.volume=db"},
    {"Name": "home_vaultwarden_data", "Driver": "local", "Labels": ""},
    {"Name": "vault_cache", "Driver": "local", "Labels": ""},
    {"Name": "home_extra", "Driver": "local", "Labels": "com.docker.compose.project=home"},
    {"Name": "home_other", "Driver": "local", "Labels": "com.docker.compose.project=home"},
    {"Name": "retired_data", "Driver": "local", "Labels": "com.docker.compose.project=retired"},
    {"Name": "loose", "Driver": "local", "Labels": ""},
    {"Name": "a" * 64, "Driver": "local", "Labels": ANON},
    {"Name": "b" * 64, "Driver": "local", "Labels": ANON},
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
MULTI_TAG = "Error response from daemon: conflict: unable to delete 777777777777 (must be forced) - " \
            "image is referenced in multiple repositories"


def lines(rows: list[dict]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def answer(fake_host, containers=CONTAINERS, volumes=VOLUMES):
    fake_host.respond("ps", stdout="".join(c["Id"] + "\n" for c in containers))
    fake_host.respond("inspect", stdout=json.dumps(containers))
    fake_host.respond("image", "ls", stdout=lines(IMAGES))
    fake_host.respond("volume", "ls", stdout=lines(volumes))
    fake_host.respond("network", "ls", stdout=lines(NETWORKS))
    for removal in (("rm",), ("image", "rm"), ("network", "rm"), ("volume", "rm")):
        fake_host.respond(*removal)


def commit_and_push(repo, message="x"):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "-q", "--no-verify", "--allow-empty", "-m", message)
    git(repo, "push", "-q", "origin", "HEAD:main")


@pytest.fixture
def host(repo, fake_host, tmp_path):
    """The fake Host, and the fixture repo as a clone whose origin/main declares STACKS and VAULT."""
    write(repo, VAULT)
    subprocess.check_call(["git", "init", "-q", "--bare", "-b", "main", str(tmp_path / "origin.git")])
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", str(tmp_path / "origin.git"))
    commit_and_push(repo)
    answer(fake_host)
    return fake_host


def cid(name: str) -> str:
    return next(c["Id"] for c in CONTAINERS if c["Name"] == "/" + name)


MULTIPLE_TAGS = "multiple tags, remove manually: {}"
KEPT_STATE = "{}: only created, exited or dead containers are removed"
UNKNOWN = "Compose project {} is no declared Stack: ownership unknown"
UNLABELLED = "named, unlabelled and undeclared: ownership unknown"


def test_strays_json_classifies_each_kind(hops, host):
    outcome = hops("strays", "--json")
    assert outcome.code == 0, outcome.err
    report = json.loads(outcome.out)

    assert report["declared_from"] == "checkout"
    assert report["containers"] == [
        {"container": "adhoc", "id": cid("adhoc"), "image": "adhoc:1", "state": "running",
         "stack": None, "service": None, "deliberate": False, "kept": KEPT_STATE.format("running"),
         "removed": None},
        {"container": "frozen", "id": cid("frozen"), "image": "adhoc:1", "state": "paused",
         "stack": None, "service": None, "deliberate": False, "kept": KEPT_STATE.format("paused"),
         "removed": None},
        {"container": "helper", "id": cid("helper"), "image": "alpine:3", "state": "dead",
         "stack": None, "service": None, "deliberate": True, "kept": None, "removed": None},
        {"container": "old-app", "id": cid("old-app"), "image": "old:1", "state": "exited",
         "stack": "retired", "service": "app", "deliberate": False, "kept": None, "removed": None},
        {"container": "spike", "id": cid("spike"), "image": "beyla:2", "state": "running",
         "stack": None, "service": None, "deliberate": True, "kept": KEPT_STATE.format("running"),
         "removed": None},
    ]
    # alpine is used only by Strays that --apply removes; adhoc and beyla by kept ones; mariadb by a
    # stopped declared Service; vaultwarden/server by a declared container under a legacy project.
    assert report["images"] == [
        {"id": img(3), "repository": "alpine", "refs": ["alpine:3"], "size": "8MB", "created": "2026-10-03",
         "kept": None, "removed": None},
        {"id": img(6), "repository": "old", "refs": [], "size": "90MB", "created": "2026-09-01",
         "kept": None, "removed": None},
        {"id": img(7), "repository": "foo", "refs": ["foo:1", "foo:latest"], "size": "10MB",
         "created": "2026-09-02", "kept": MULTIPLE_TAGS.format("foo:1 foo:latest"), "removed": None},
    ]
    # Never listed: the stopped mariadb's unmounted volume (its Stack's), volumes a Stack names,
    # home_* (a kept container's legacy project), and the anonymous volume the kept spike mounts.
    assert report["volumes"] == [
        {"volume": "b" * 64, "stack": None, "anonymous": True, "kept": None, "removed": None},
        {"volume": "loose", "stack": None, "anonymous": False, "kept": UNLABELLED, "removed": None},
        {"volume": "retired_data", "stack": "retired", "anonymous": False,
         "kept": UNKNOWN.format("retired"), "removed": None},
    ]
    assert report["networks"] == [
        {"network": "manual-net", "id": "n-manual", "stack": None, "kept": None, "removed": None},
        {"network": "retired_default", "id": "n-retired", "stack": "retired", "kept": None, "removed": None},
    ]


def test_anonymous_volume_left_by_compose_down(hops, host):
    # `compose down` removed nextcloud's containers; Docker retained their anonymous volumes. Compose
    # versions that label them with the project keep them their Stack's. Unlabelled ones (Compose
    # 2.38 on the Host) and ones labelled with an unknown project are handled like any other volume.
    left = [
        {"Name": "c" * 64, "Driver": "local", "Labels": ANON + ",com.docker.compose.project=nextcloud"},
        {"Name": "d" * 64, "Driver": "local", "Labels": ANON + ",com.docker.compose.project=gone"},
        {"Name": "e" * 64, "Driver": "local", "Labels": ANON},
    ]
    answer(host, containers=CONTAINERS[:1], volumes=left)
    volumes = {v["volume"][0]: v["kept"] for v in json.loads(hops("strays", "--json").out)["volumes"]}
    assert volumes == {"d": UNKNOWN.format("gone"), "e": None}


def test_default_run_only_reads_and_says_the_checkout_is_the_source(hops, host):
    out = hops("strays").out
    assert host.docker_calls == READS
    assert "this checkout" in out and "origin/main" in out


def test_table_lists_strays_and_says_nothing_was_removed(hops, host):
    out = hops("strays").out
    assert "adhoc" in out and "old-app" in out and "manual-net" in out and "retired_data" in out
    assert "deliberate docker run (startup-order=skip)" in out
    assert "kept: running: only created, exited or dead" in out
    assert "kept: Compose project retired is no declared Stack" in out
    assert "old (untagged)" in out
    assert "Nothing removed" in out and "--volumes" in out
    for kept in ("traefik", "mariadb", "vaultwarden", "home_", "ix-foo", "vault_cache", "proxy", "bridge"):
        assert kept not in out, kept


def test_apply_removes_stopped_containers_images_networks_but_never_volumes(hops, host):
    outcome = hops("strays", "--apply")
    assert outcome.code == 0, outcome.err
    assert host.docker_calls[len(READS):] == [
        ["rm", cid("helper")],
        ["rm", cid("old-app")],
        ["image", "rm", img(3)],
        ["image", "rm", img(6)],
        ["network", "rm", "n-manual"],
        ["network", "rm", "n-retired"],
    ]
    assert "origin/main" in outcome.out


def test_apply_with_volumes_removes_only_volumes_nobody_could_own(hops, host):
    outcome = hops("strays", "--apply", "--volumes", "--json")
    assert outcome.code == 0, outcome.err
    report = json.loads(outcome.out)
    assert [c for c in host.docker_calls if c[:2] == ["volume", "rm"]] == [["volume", "rm", "b" * 64]]
    assert [c["removed"] for c in report["containers"]] == [None, None, True, True, None]
    assert [v["removed"] for v in report["volumes"]] == [True, None, None]
    assert report["declared_from"].startswith("origin/main@")


def test_apply_never_removes_an_image_by_tag(hops, host):
    # alpine:3 gained a second tag after the list was read: Docker refuses the ID, and hops keeps it
    # rather than untag mutable references a concurrent pull may have moved.
    host.respond("image", "rm", img(3), stderr=MULTI_TAG, returncode=1)
    outcome = hops("strays", "--apply", "--json")
    assert outcome.code == 0, outcome.err
    assert [c for c in host.docker_calls if c[:1] == ["image"] and c[1] != "ls"] == [
        ["image", "rm", img(3)], ["image", "rm", img(6)]]
    images = json.loads(outcome.out)["images"]
    assert images[0]["kept"] == MULTIPLE_TAGS.format("alpine:3 and tags added since it was listed")
    assert images[0]["removed"] is None


def test_anonymous_volume_of_a_removed_stray_waits_for_a_later_run(hops, host):
    # The db Stack was renamed from old to new: old-db-1 stays behind, exited, with its anonymous
    # data volume. The container goes; the volume stays until a run finds nothing referencing it.
    left = on_host(container("old-db-1", project="old", service="db", status="exited", image="old:1"),
                   image_id=img(6), volumes=["f" * 64])
    answer(host, containers=[*CONTAINERS, left], volumes=[*VOLUMES, {"Name": "f" * 64, "Labels": ANON}])
    report = json.loads(hops("strays", "--apply", "--volumes", "--json").out)
    assert ["rm", left["Id"]] in host.docker_calls
    assert ["volume", "rm", "f" * 64] not in host.docker_calls
    volume = next(v for v in report["volumes"] if v["volume"] == "f" * 64)
    assert volume["kept"] == "used by Stray container old-db-1: removable once nothing references it"

    answer(host, volumes=[*VOLUMES, {"Name": "f" * 64, "Labels": ANON}])  # the next run
    hops("strays", "--apply", "--volumes")
    assert ["volume", "rm", "f" * 64] in host.docker_calls


def test_apply_uses_origin_main_not_a_partial_checkout(hops, host, repo):
    # This checkout lost tn/vault and tn/nextcloud; origin/main still declares them.
    for stack in ("vault", "nextcloud"):
        subprocess.check_call(["rm", "-r", str(repo / "tn" / stack)])
    assert "home_other" in hops("strays", "--json").out  # list mode reads the checkout, and says so
    report = json.loads(hops("strays", "--apply", "--volumes", "--json").out)
    assert ["rm", cid("mariadb")] not in host.docker_calls
    assert [c for c in host.docker_calls if c[:2] == ["volume", "rm"]] == [["volume", "rm", "b" * 64]]
    assert "mariadb" not in [c["container"] for c in report["containers"]]


def test_apply_fetches_origin_main_first(hops, host, repo, tmp_path):
    # Another clone retires the vault Stack on origin/main after this checkout last fetched.
    other = tmp_path / "other"
    subprocess.check_call(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)])
    subprocess.check_call(["rm", "-r", str(other / "tn" / "vault")])
    commit_and_push(other, "retire vault")
    report = json.loads(hops("strays", "--apply", "--json").out)
    assert "vaultwarden" in [c["container"] for c in report["containers"]]


def test_apply_fetches_main_whatever_the_fetch_refspec_and_says_which_commit(hops, host, repo, tmp_path):
    git(repo, "config", "remote.origin.fetch", "+refs/heads/other:refs/remotes/origin/other")
    other = tmp_path / "other"
    subprocess.check_call(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)])
    subprocess.check_call(["rm", "-r", str(other / "tn" / "vault")])
    commit_and_push(other, "retire vault")
    sha = subprocess.check_output(["git", "-C", str(other), "rev-parse", "HEAD"], text=True).strip()
    report = json.loads(hops("strays", "--apply", "--json").out)
    assert "vaultwarden" in [c["container"] for c in report["containers"]]
    assert report["declared_from"] == f"origin/main@{sha}"
    assert sha in hops("strays", "--apply").out


def test_apply_refuses_when_origin_main_declares_no_services(hops, host, repo):
    subprocess.check_call(["git", "rm", "-q", "-r", "tn"], cwd=repo)
    write(repo, {"tn/README.md": "empty\n"})
    commit_and_push(repo, "empty tn")
    outcome = hops("strays", "--apply")
    assert outcome.code == 2
    assert "origin/main" in outcome.err and "no Services" in outcome.err
    assert host.calls == []


def test_volumes_without_apply_is_a_usage_error(hops, fake_host):
    outcome = hops("strays", "--volumes")
    assert outcome.code == 2
    assert "--volumes needs --apply" in outcome.err
    assert fake_host.calls == []


def test_apply_keeps_going_after_a_failed_removal_and_exits_1(hops, host):
    host.respond("rm", cid("old-app"), stderr="Error: permission denied", returncode=1)
    outcome = hops("strays", "--apply", "--json")
    assert outcome.code == 1
    assert "old-app" in outcome.err and "permission denied" in outcome.err
    report = json.loads(outcome.out)
    assert [c["removed"] for c in report["containers"]] == [None, None, True, False, None]
    assert ["network", "rm", "n-retired"] in host.docker_calls


def test_apply_volumes_refuses_when_a_declared_volume_name_uses_a_variable(hops, host, repo):
    write(repo, {"tn/vault/compose.yaml": VAULT["tn/vault/compose.yaml"].replace(
        "name: home_vaultwarden_data", "name: ${PREFIX}_vaultwarden_data")})
    commit_and_push(repo, "variable volume name")
    outcome = hops("strays", "--apply", "--volumes")
    assert outcome.code == 2
    assert "${PREFIX}_vaultwarden_data" in outcome.err
    assert host.calls == []


def test_no_strays(hops, fake_host):
    fake_host.respond("ps", stdout="")
    for kind in ("image", "volume", "network"):
        fake_host.respond(kind, "ls", stdout="")
    outcome = hops("strays")
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


# Expired Rollback Snapshots (hops.snapshots, through TrueNAS)

DOCKER = "luna/ix-apps/docker"


@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(snapshots, "now", lambda: datetime(2026, 10, 23, 12, tzinfo=timezone.utc))


@pytest.mark.usefixtures("today")
def test_strays_reports_expired_rollback_snapshots(hops, fake_host):
    answer(fake_host)
    pool = FakeSnapshots(rollback(f"{DOCKER}@hops-1-x", issue="1", expires="2026-10-22"),
                         rollback(f"{DOCKER}@hops-2-x", issue="2", expires="2026-10-23"),
                         snap(f"{DOCKER}@luna-2026-10-19_23-00"))
    outcome = hops("strays", "--json", truenas=pool.client)
    assert outcome.code == 0, outcome.err
    report = json.loads(outcome.out)
    assert report["snapshots"] == [
        {"snapshot": f"{DOCKER}@hops-1-x", "dataset": DOCKER, "issue": "1", "expires": "2026-10-22", "size": 1024,
         "kept": "released only with hops snapshot release, once its change is verified", "removed": None}]
    assert report["snapshots_unchecked"] is None
    text = hops("strays", truenas=pool.client).out
    assert "Expired Rollback Snapshots:" in text and "hops-2-x" not in text


@pytest.mark.usefixtures("host", "today")
def test_strays_apply_never_releases_a_snapshot(hops):
    pool = FakeSnapshots(rollback(f"{DOCKER}@hops-1-x", expires="2026-10-01"))
    outcome = hops("strays", "--apply", "--json", truenas=pool.client)
    assert outcome.code == 0, outcome.err
    assert json.loads(outcome.out)["snapshots"][0]["removed"] is None
    assert pool.called("pool.snapshot.delete") == []


def test_strays_without_truenas_says_snapshots_were_not_checked(hops, fake_host):
    answer(fake_host)
    outcome = hops("strays", "--json")
    assert outcome.code == 0, outcome.err
    report = json.loads(outcome.out)
    assert report["snapshots"] is None
    assert report["snapshots_unchecked"].startswith("TrueNAS is not available on this host")
    assert "Rollback Snapshots not checked: TrueNAS is not available" in hops("strays").out
