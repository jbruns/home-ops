"""hops inspect <svc>: why a Service's container is in the state it is."""
import json

from hops_fakes import container

LOG = [{"Start": "2026-10-21T21:00:00Z", "End": "2026-10-21T21:00:01Z", "ExitCode": 1,
        "Output": "connection refused\n"}]


def test_inspect_running_service_reads_health_log_and_memory(hops, fake_host):
    mariadb = container("mariadb", project="nextcloud", service="mariadb", image="mariadb:11.8.9@sha256:bbbb",
                        health="unhealthy", health_log=LOG, restarts=2, memory=1610612736)
    fake_host.respond("inspect", stdout=json.dumps([mariadb]))
    fake_host.respond("stats", stdout=json.dumps({"Name": "mariadb", "MemUsage": "512MiB / 1.5GiB",
                                                  "MemPerc": "33.33%"}) + "\n")
    outcome = hops("inspect", "mariadb", "--json")

    assert outcome.code == 0
    assert fake_host.docker_calls == [["inspect", "mariadb"], ["stats", "--no-stream", "--format", "json", "mariadb"]]
    assert json.loads(outcome.out) == {
        "stack": "nextcloud", "service": "mariadb", "container": "mariadb",
        "image": "mariadb:11.8.9@sha256:bbbb", "state": "running", "health": "unhealthy",
        "started_at": "2026-10-21T20:00:00.000000000Z", "exit_code": 0, "oom_killed": False, "restart_count": 2,
        "memory": {"usage_bytes": 536870912, "limit_bytes": 1610612736, "percent": 33.3},
        "health_log": [{"start": "2026-10-21T21:00:00Z", "end": "2026-10-21T21:00:01Z", "exit_code": 1,
                        "output": "connection refused\n"}],
    }


def test_inspect_stopped_service_reports_exit_code_and_oom_kill_without_stats(hops, fake_host):
    valkey = container("valkey", project="nextcloud", service="valkey", status="exited", health="unhealthy",
                       exit_code=137, oom_killed=True)
    fake_host.respond("inspect", stdout=json.dumps([valkey]))
    report = json.loads(hops("inspect", "valkey", "--json").out)
    assert (report["exit_code"], report["oom_killed"], report["memory"]) == (137, True, None)
    assert fake_host.docker_calls == [["inspect", "valkey"]]


def test_inspect_finds_a_container_without_container_name_by_compose_labels(hops, fake_host):
    doco = container("doco-cd-doco-cd-1", project="doco-cd", service="doco-cd", health=None)
    fake_host.respond("ps", stdout=doco["Id"] + "\n")
    fake_host.respond("inspect", stdout=json.dumps([doco]))
    fake_host.respond("stats", stdout=json.dumps({"MemUsage": "20MiB / 31.2GiB"}) + "\n")
    report = json.loads(hops("inspect", "doco-cd", "--json").out)
    assert fake_host.docker_calls[:2] == [
        ["ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=doco-cd",
         "--filter", "label=com.docker.compose.service=doco-cd"],
        ["inspect", doco["Id"]],
    ]
    assert report["container"] == "doco-cd-doco-cd-1"
    assert report["memory"] == {"usage_bytes": 20971520, "limit_bytes": None, "percent": None}


def test_inspect_missing_container_fails(hops, fake_host):
    fake_host.respond("inspect", returncode=1, stderr="Error: No such object: valkey\n")
    outcome = hops("inspect", "valkey")
    assert outcome.code == 1
    assert "nextcloud/valkey has no container on the Host (expected valkey)" in outcome.err


def test_inspect_prints_a_readable_summary(hops, fake_host):
    mariadb = container("mariadb", project="nextcloud", service="mariadb", health="unhealthy", health_log=LOG,
                        memory=1610612736)
    fake_host.respond("inspect", stdout=json.dumps([mariadb]))
    fake_host.respond("stats", stdout=json.dumps({"MemUsage": "512MiB / 1.5GiB"}) + "\n")
    out = hops("inspect", "nextcloud/mariadb").out
    assert "memory:" in out and "512.0MiB / 1.5GiB (33.3%)" in out
    assert "connection refused" in out
