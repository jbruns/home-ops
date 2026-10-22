"""hops status: declared vs running for each Service, with digest drift."""
import json

from hops_fakes import container

CONTAINERS = [
    container("traefik", project="edge", service="traefik", image="traefik:v3.7.15@sha256:aaaa"),
    # A Deploy that silently failed: the repo pins sha256:bbbb, the container still runs the old one.
    container("mariadb", project="nextcloud", service="mariadb", image="mariadb:11.8.9@sha256:0ld"),
    container("nextcloud", project="nextcloud", service="nextcloud", image="nextcloud:35.0.2@sha256:dddd",
              status="exited", health="unhealthy", restarts=3, exit_code=1),
    container("nextcloud-exporter", project="nextcloud", service="exporter", health=None,
              image="prom/mysqld-exporter:v0.20.0@sha256:cccc"),
    # No container_name: found by its Compose labels, whatever number Compose gave it.
    container("doco-cd-doco-cd-2", project="doco-cd", service="doco-cd",
              image="ghcr.io/kimdre/doco-cd:0.128.0@sha256:1111"),
    container("sonarr", project="sonarr", service="sonarr", image="ghcr.io/home-operations/sonarr:4.0.20.3012"),
    container("sonarr-exporter", project="sonarr", service="exporter", image="ghcr.io/onedr0p/exportarr:v2.3.0"),
    container("adhoc", image="alpine:3", health=None),
    container("old-app", project="retired", service="app", image="old:1", health=None),
    container("stopped-adhoc", image="alpine:3", status="exited", health=None),
]


def respond_with(fake_host, containers):
    fake_host.respond("ps", stdout="".join(c["Id"] + "\n" for c in containers))
    fake_host.respond("inspect", stdout=json.dumps(containers))


def test_status_reads_every_container_in_two_host_calls(hops, fake_host):
    respond_with(fake_host, CONTAINERS)
    assert hops("status", "--json").code == 0
    assert fake_host.docker_calls == [
        ["ps", "-aq", "--no-trunc"],
        ["inspect", *(c["Id"] for c in CONTAINERS)],
    ]


def test_status_json_reports_state_health_restarts_and_digest_drift(hops, fake_host):
    respond_with(fake_host, CONTAINERS)
    report = json.loads(hops("status", "--json").out)
    rows = {r["stack"] + "/" + r["service"]: r for r in report["services"]}

    assert rows["edge/traefik"] == {
        "stack": "edge", "service": "traefik", "tier": "critical", "container": "traefik",
        "state": "running", "health": "healthy", "restart_count": 0,
        "declared_image": "traefik:v3.7.15@sha256:aaaa", "running_image": "traefik:v3.7.15@sha256:aaaa",
        "digest_drift": False,
    }
    assert rows["nextcloud/mariadb"]["digest_drift"] is True
    assert rows["nextcloud/mariadb"]["running_image"] == "mariadb:11.8.9@sha256:0ld"
    assert {k: rows["nextcloud/nextcloud"][k] for k in ("state", "health", "restart_count")} == {
        "state": "exited", "health": "unhealthy", "restart_count": 3}
    assert rows["nextcloud/exporter"]["health"] is None
    assert rows["nextcloud/valkey"] == {
        "stack": "nextcloud", "service": "valkey", "tier": "critical", "container": "valkey",
        "state": "missing", "health": None, "restart_count": None,
        "declared_image": "valkey/valkey:9.1.2-alpine@sha256:eeee", "running_image": None,
        "digest_drift": None,
    }
    assert rows["doco-cd/doco-cd"]["container"] == "doco-cd-doco-cd-2"
    # Nothing to compare: the container runs no digest, or the repo pins none.
    assert rows["sonarr/sonarr"]["digest_drift"] is None
    assert rows["sonarr/exporter"]["digest_drift"] is None

    assert report["undeclared"] == [
        {"container": "adhoc", "image": "alpine:3", "state": "running", "stack": None, "service": None},
        {"container": "old-app", "image": "old:1", "state": "running", "stack": "retired", "service": "app"},
    ]


def test_status_targets_limit_the_services_and_skip_undeclared(hops, fake_host):
    respond_with(fake_host, CONTAINERS)
    report = json.loads(hops("status", "edge", "nextcloud/mariadb", "--json").out)
    assert [(r["stack"], r["service"]) for r in report["services"]] == [("edge", "traefik"), ("nextcloud", "mariadb")]
    assert report["undeclared"] == []


def test_status_prefers_the_running_container_of_a_service(hops, fake_host):
    old = container("doco-cd-doco-cd-1", project="doco-cd", service="doco-cd", status="exited", image="x@sha256:1")
    new = container("doco-cd-doco-cd-2", project="doco-cd", service="doco-cd", image="x@sha256:2")
    respond_with(fake_host, [old, new])
    rows = json.loads(hops("status", "doco-cd", "--json").out)["services"]
    assert (rows[0]["container"], rows[0]["state"]) == ("doco-cd-doco-cd-2", "running")


def test_status_with_no_containers_skips_inspect(hops, fake_host):
    fake_host.respond("ps", stdout="")
    report = json.loads(hops("status", "edge", "--json").out)
    assert report["services"][0]["state"] == "missing"
    assert fake_host.docker_calls == [["ps", "-aq", "--no-trunc"]]


def test_status_table_flags_drift_and_problems(hops, fake_host):
    respond_with(fake_host, CONTAINERS)
    out = hops("status").out
    assert "nextcloud/mariadb" in out and "DRIFT" in out
    assert "runs mariadb:11.8.9@sha256:0ld, the repo pins mariadb:11.8.9@sha256:bbbb" in out
    assert "nextcloud/valkey" in out and "missing" in out
    assert "adhoc" in out and "old-app" in out and "stopped-adhoc" not in out


def test_unknown_target_fails_before_any_host_call(hops, fake_host):
    outcome = hops("status", "nope")
    assert outcome.code == 2
    assert "unknown Stack or Service 'nope'" in outcome.err
    assert fake_host.calls == []


def test_status_survives_a_container_removed_between_ps_and_inspect(hops, fake_host):
    # A Deploy recreating a container mid-run: inspect prints what it found and exits 1.
    fake_host.respond("ps", stdout="gone\n" + CONTAINERS[0]["Id"] + "\n")
    fake_host.respond("inspect", stdout=json.dumps(CONTAINERS[:1]), stderr="Error: No such object: gone",
                      returncode=1)
    result = hops("status", "--json", "edge")
    assert result.code == 0
    assert json.loads(result.out)["services"][0]["state"] == "running"


def test_status_drift_message_names_the_checkout_not_a_failed_deploy(hops, fake_host):
    respond_with(fake_host, CONTAINERS)
    out = hops("status", "nextcloud/mariadb").out
    # Declared state is read from the current checkout, which may be a branch doco-cd does not deploy.
    assert "this checkout pins" in out
    assert "or this checkout is not what doco-cd deploys" in out
