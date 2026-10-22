"""hops restart, start, stop --for and recreate: runtime state only, with the Disruptive guardrails."""
import json
import re
from pathlib import Path

import pytest
import yaml

from hops import doco
from hops import silences as silences_module
from hops.declared import Declared
from hops.errors import HopsError, HostTimeout
from hops_fakes import container

KEY = "fake-doco-cd-key"
PASSWORD = ["exec", "prometheus", "cat", "/run/secrets/prometheus_password"]


def amtool_args(call):
    """The arguments the remote sh passes to amtool ("$@" after $0=amtool)."""
    alertmanager = call.docker[1]
    return alertmanager[alertmanager.index("amtool") + 1:]


@pytest.fixture
def api_key(monkeypatch):
    """Stands in for `sops -d`; records the file it was asked to decrypt."""
    asked = []

    def decrypt(path):
        asked.append(path)
        return KEY + "\n"

    monkeypatch.setattr(doco, "decrypt", decrypt)
    return asked


def http_response(status: str, body: dict) -> str:
    """doco-cd's answer as the Runner returns it: text mode turns its CRLFs into LFs."""
    text = json.dumps(body)
    return f"HTTP/1.0 {status}\nContent-Type: application/json\nContent-Length: {len(text)}\n\n{text}"


# restart and start


def test_restart_prints_the_containers_then_restarts_them_in_one_call(hops, fake_host):
    fake_host.respond("restart")
    outcome = hops("restart", "nextcloud")
    assert outcome.code == 0
    assert fake_host.docker_calls == [["restart", "nextcloud-exporter", "mariadb", "nextcloud", "valkey"]]
    assert "nextcloud/mariadb (mariadb)" in outcome.out
    assert outcome.out.index("nextcloud/valkey") < outcome.out.index("Restarted")


def test_start_finds_a_container_without_container_name_by_its_compose_labels(hops, fake_host):
    fake_host.respond("ps", stdout="abc\n")
    fake_host.respond("inspect", "abc", stdout=json.dumps([container("doco-cd-doco-cd-1", status="exited")]))
    fake_host.respond("start")
    assert hops("start", "doco-cd").code == 0
    assert fake_host.docker_calls[-1] == ["start", "doco-cd-doco-cd-1"]


def test_lifecycle_rejects_an_unknown_name_before_touching_the_host(hops, fake_host):
    outcome = hops("restart", "sonar")
    assert outcome.code == 2 and "did you mean sonarr" in outcome.err
    assert fake_host.calls == []


def test_restart_reports_a_docker_failure(hops, fake_host):
    fake_host.respond("restart", returncode=1, stderr="Error response from daemon: boom")
    outcome = hops("restart", "sonarr/sonarr")
    assert outcome.code == 1 and "boom" in outcome.err


# stop


def test_stop_without_for_is_refused_before_touching_the_host(hops, fake_host):
    outcome = hops("stop", "sonarr/sonarr")
    assert outcome.code == 2 and "--for" in outcome.err
    assert fake_host.calls == []


@pytest.mark.parametrize("duration", ["2", "1 h", "0h", "-1h", "1x", "h"])
def test_stop_rejects_a_bad_duration(hops, fake_host, duration):
    outcome = hops("stop", "sonarr/sonarr", "--for", duration)
    assert outcome.code == 2 and "--for" in outcome.err
    assert fake_host.calls == []


def test_stop_of_a_critical_service_needs_force(hops, fake_host):
    outcome = hops("stop", "traefik", "--for", "30m")
    assert outcome.code == 2 and "Critical" in outcome.err and "--force" in outcome.err
    assert fake_host.calls == []


def test_stop_of_a_stack_with_any_critical_service_needs_force(hops, fake_host):
    outcome = hops("stop", "nextcloud", "--for", "30m")
    assert outcome.code == 2 and "nextcloud/mariadb" in outcome.err
    assert fake_host.calls == []


def silences(fake_host):
    """The matchers of each `amtool silence add`, in order."""
    found = []
    for call in fake_host.calls:
        if call.docker[0] == PASSWORD:
            args = amtool_args(call)
            assert args[:2] == ["silence", "add"]
            found.append([a for a in args[2:] if not a.startswith("--")])
    return found


def test_stop_silences_the_service_then_stops_it(hops, fake_host):
    fake_host.respond("exec", "-i", "alertmanager", stdout="silence-1\n")
    fake_host.respond("stop")
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h30m")
    assert outcome.code == 0
    assert silences(fake_host) == [['stack="sonarr"', 'service="sonarr"'], ['container="sonarr"'], ['name="sonarr"']]
    for call in fake_host.calls[:-1]:
        args = amtool_args(call)
        assert "--duration=1h30m" in args
        assert any(a.startswith("--comment=hops stop sonarr/sonarr") for a in args)
    assert fake_host.calls[-1].docker == [["stop", "sonarr"]]
    assert 'Silenced container="sonarr" for 1h30m (silence-1): sonarr/sonarr: alerts on its container' in outcome.out
    assert "Nothing restarts" in outcome.out and "hops start sonarr/sonarr" in outcome.out


def test_stop_of_a_critical_service_with_force(hops, fake_host):
    fake_host.respond("exec", "-i", "alertmanager", stdout="silence-2\n")
    fake_host.respond("stop")
    assert hops("stop", "traefik", "--for", "10m", "--force").code == 0
    assert silences(fake_host)[0] == ['stack="edge"', 'service="traefik"']
    assert fake_host.docker_calls[-1] == ["stop", "traefik"]


def test_stop_does_not_stop_when_the_silence_fails(hops, fake_host):
    fake_host.respond("exec", "-i", "alertmanager", returncode=1, stderr="amtool: unauthorized")
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h")
    assert outcome.code == 1 and "unauthorized" in outcome.err
    assert [c for c in fake_host.docker_calls if c[0] == "stop"] == []


# What the silences of `stop` cover, checked against the repo's real alert rules and scrape config.
REPO = Path(__file__).resolve().parents[3]
MONITORING = REPO / "tn" / "monitoring" / "prometheus"


def rule(name: str) -> dict:
    groups = yaml.safe_load((MONITORING / "alerting-rules.yml").read_text())["groups"]
    return next(r for g in groups for r in g["rules"] if r.get("alert") == name)


def scrape_target(job: str, **having) -> dict:
    """The labels Prometheus gives a static target's series: job, instance and the target labels."""
    config = yaml.safe_load((MONITORING / "prometheus.yml").read_text())
    for sc in config["scrape_configs"]:
        for static in sc.get("static_configs") or []:
            labels = static.get("labels") or {}
            if sc["job_name"] == job and having.items() <= labels.items():
                return {"job": job, "instance": static["targets"][0], **labels}
    raise AssertionError(f"no {job} target with {having}")


def alert(name: str, series: dict) -> dict:
    """The labels the alert carries for a series: its expr's label_replace copies, then the rule's labels."""
    r = rule(name)
    labels = dict(series)
    for dst, src in re.findall(r'"(\w+)", "\$1", "(\w+)", "\(\.\+\)"', r["expr"]):
        if src in labels:
            labels[dst] = labels[src]
    return {**labels, **(r.get("labels") or {}), "alertname": name}


def silenced(matcher_sets: list[dict], labels: dict) -> bool:
    return any(m.items() <= labels.items() for m in matcher_sets)


def stop_plan(*stopped: tuple[str, str]) -> list:
    """The silences `hops stop` would create for (stack/service, container) pairs, from the real repo."""
    declared = Declared.load(REPO)
    found = [(declared.resolve_one(key), container) for key, container in stopped]
    return silences_module.plan(declared, found, silences_module.Monitoring.load(REPO, "tn"))


def stop_silences(key: str, container: str) -> list[dict]:
    return [s.matchers for s in stop_plan((key, container))]


def probe_failed(stack: str, service: str, container: str, job: str = "probe", **labels) -> dict:
    """ProbeFailed as the probe jobs relabel it from the probed container's Compose labels."""
    service_ = Declared.load(REPO).get(stack, service)
    return alert("ProbeFailed", {"job": job, "instance": "https://example", "container": container,
                                 "service": service, "stack": stack, "tier": service_.tier, **labels})


def test_stopping_traefik_silences_its_target_down_page():
    found = stop_silences("edge/traefik", "traefik")
    assert silenced(found, alert("TargetDown", scrape_target("traefik")))
    assert not silenced(found, alert("TargetDown", scrape_target("crowdsec")))


def test_stopping_a_datastore_silences_datastore_down():
    found = stop_silences("nextcloud/mariadb", "mariadb")
    assert silenced(found, alert("DatastoreDown", scrape_target("mysqld", datastore="mariadb")))
    assert not silenced(found, alert("DatastoreDown", scrape_target("mysqld", datastore="vaultwarden-db")))


def test_stopping_a_service_without_container_name_silences_its_target_by_service_name():
    found = stop_silences("doco-cd/doco-cd", "doco-cd-doco-cd-6")
    assert silenced(found, alert("TargetDown", scrape_target("doco-cd")))


def test_stopping_a_service_silences_container_down():
    found = stop_silences("sonarr/sonarr", "sonarr")
    assert silenced(found, alert("ContainerDown", {"stack": "sonarr", "service": "sonarr", "name": "sonarr",
                                                   "tier": "warning"}))


def test_stopping_traefik_silences_every_probe_its_own_missing_probe_no_longer_inhibits():
    """Docker SD drops a stopped container's Probe, so ProbeFailed{container="traefik"} never fires."""
    plan = stop_plan(("edge/traefik", "traefik"))
    found = [s.matchers for s in plan]
    assert silenced(found, probe_failed("vaultwarden", "vaultwarden", "vaultwarden"))
    assert silenced(found, probe_failed("nextcloud", "nextcloud", "nextcloud"))
    assert silenced(found, probe_failed("calibre-web-automated", "calibre-web-automated", "calibre-web-automated",
                                        job="probe-idp", auth="forward"))
    assert silenced(found, alert("IdpForwardAuthFailing", {}))
    (every_probe,) = [s for s in plan if s.matchers == {"alertname": "ProbeFailed"}]
    assert "all Probes" in every_probe.reason


def test_stopping_other_services_leaves_other_probes_paging():
    for key, container in [("sonarr/sonarr", "sonarr"), ("edge/crowdsec", "crowdsec"), ("nextcloud/mariadb", "mariadb")]:
        found = stop_silences(key, container)
        assert not silenced(found, probe_failed("vaultwarden", "vaultwarden", "vaultwarden")), key


def test_stopping_a_datastore_silences_the_services_in_its_stack_that_depend_on_it():
    found = stop_silences("vaultwarden/vaultwarden-db", "vaultwarden-db")
    assert silenced(found, probe_failed("vaultwarden", "vaultwarden", "vaultwarden"))
    assert silenced(found, alert("TargetDown", scrape_target("mysqld", datastore="vaultwarden-db")))
    nextcloud = stop_silences("nextcloud/mariadb", "mariadb")
    assert silenced(nextcloud, probe_failed("nextcloud", "nextcloud", "nextcloud"))
    assert silenced(nextcloud, alert("ContainerDown", {"stack": "nextcloud", "service": "nextcloud-cron",
                                                       "name": "nextcloud-cron", "tier": "warning"}))
    assert not silenced(found, probe_failed("nextcloud", "nextcloud", "nextcloud"))


def test_stopping_a_service_silences_its_stacks_alerts_that_name_no_service():
    found = stop_silences("ops/stack-back", "stack-back")
    assert silenced(found, alert("BackupStale", {"kind": "volumes"}))
    assert silenced(found, alert("BackupMetricsMissing", {}))
    assert not silenced(stop_silences("sonarr/sonarr", "sonarr"), alert("BackupStale", {"kind": "volumes"}))


def test_stopping_a_monitoring_service_never_silences_the_heartbeat():
    found = stop_silences("monitoring/grafana", "grafana")
    assert not silenced(found, alert("Watchdog", {}))


@pytest.mark.parametrize("key, container", [("edge/traefik", "traefik"), ("ops/stack-back", "stack-back"),
                                            ("vaultwarden/vaultwarden-db", "vaultwarden-db"),
                                            ("monitoring/prometheus", "prometheus")])
def test_every_silence_is_narrow_and_explained(key, container):
    for s in stop_plan((key, container)):
        assert s.matchers and set(s.matchers) != {"stack"} and s.reason, s


# recreate

# On the metrics network: prometheus, traefik, sonarr's exporter (no nc -e), and a stopped alertmanager.
METRICS = [
    container("prometheus", project="monitoring", service="prometheus"),
    container("traefik", project="edge", service="traefik"),
    container("sonarr-exporter", project="sonarr", service="exporter"),
    container("alertmanager", project="monitoring", service="alertmanager", status="exited"),
]


@pytest.fixture
def metrics(fake_host):
    """The metrics network; every member but sonarr-exporter passes the transport probe."""
    fake_host.respond("network", "inspect", "metrics",
                      stdout=" ".join(c["Name"].lstrip("/") for c in METRICS) + " \n")
    fake_host.respond("inspect", stdout=json.dumps(METRICS))
    for c in METRICS:
        fake_host.respond("exec", c["Name"].lstrip("/"), "sh", returncode=1 if c["Name"] == "/sonarr-exporter" else 0)
    return fake_host


def requests(fake_host):
    return [c for c in fake_host.calls if c.docker[-1][:2] == ["exec", "-i"]]


def probes(fake_host):
    return [c.docker[-1][1] for c in fake_host.calls if c.docker[-1][0] == "exec" and c.docker[-1][1] != "-i"]


def test_recreate_sends_the_api_key_on_stdin_never_in_argv(hops, metrics, api_key, repo):
    metrics.respond("exec", "-i", "prometheus", stdout=http_response("200 OK", {"content": "ok"}))
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 0, outcome.err
    assert api_key == [repo / "tn" / "doco-cd" / "api_secret.sops.txt"]
    assert all(KEY not in arg for call in metrics.calls for arg in call.argv)
    (call,) = requests(metrics)
    assert call.docker[0][:4] == ["exec", "-i", "prometheus", "sh"]
    request = call.input
    assert request.startswith("POST /v1/api/project/sonarr/recreate?service=sonarr HTTP/1.0\r\n")
    assert f"\r\nx-api-key: {KEY}\r\n" in request and request.endswith("\r\n\r\n")
    assert "Recreated sonarr/sonarr" in outcome.out


def test_recreate_reports_doco_cds_error(hops, metrics, api_key):
    metrics.respond("exec", "-i", "prometheus",
                    stdout=http_response("404 Not Found", {"error": "service not found", "code": 404}))
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "404" in outcome.err and "service not found" in outcome.err


def test_recreate_fails_without_an_http_response(hops, metrics, api_key):
    metrics.respond("exec", "-i", "prometheus", returncode=1, stderr="nc: bad address 'doco-cd'")
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "bad address" in outcome.err


def test_recreate_refuses_doco_cd_itself(hops, fake_host, api_key):
    outcome = hops("recreate", "doco-cd")
    assert outcome.code == 2 and "doco-cd" in outcome.err
    assert fake_host.calls == [] and api_key == []


def test_recreate_stops_when_sops_fails(hops, metrics, monkeypatch):
    def fail(path):
        raise HopsError("sops -d failed: no key")

    monkeypatch.setattr(doco, "decrypt", fail)
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "sops" in outcome.err
    assert requests(metrics) == []


def test_recreate_of_prometheus_sends_through_another_container(hops, metrics, api_key):
    """Prometheus would stop mid-request; sonarr-exporter lacks nc -e, alertmanager is not running."""
    metrics.respond("exec", "-i", "traefik", stdout=http_response("200 OK", {}))
    assert hops("recreate", "monitoring/prometheus").code == 0
    assert probes(metrics) == ["sonarr-exporter", "traefik"]
    (call,) = requests(metrics)
    assert call.docker[0][:3] == ["exec", "-i", "traefik"]


def test_recreate_of_a_stack_picks_the_transport_once_outside_it(hops, metrics, api_key):
    metrics.respond("exec", "-i", "traefik", stdout=http_response("200 OK", {}))
    outcome = hops("recreate", "monitoring")
    assert outcome.code == 0, outcome.err
    assert [c.docker[0][2] for c in requests(metrics)] == ["traefik", "traefik"]
    first_request = metrics.calls.index(requests(metrics)[0])
    assert all(c.docker[-1][:2] == ["exec", "-i"] for c in metrics.calls[first_request:])


def test_recreate_refuses_when_no_container_can_carry_the_request(hops, metrics, api_key):
    metrics.respond("exec", "traefik", "sh", returncode=1)
    outcome = hops("recreate", "monitoring/prometheus")
    assert outcome.code == 1 and "metrics" in outcome.err
    assert api_key == [] and requests(metrics) == []


def test_recreate_bounds_the_request_on_the_host_and_here(hops, metrics, api_key):
    metrics.respond("exec", "-i", "prometheus", stdout=http_response("200 OK", {}))
    assert hops("recreate", "sonarr/sonarr", "--timeout", "90").code == 0
    (call,) = requests(metrics)
    script_args = call.docker[0][call.docker[0].index("-c") + 1:]
    assert "timeout" in script_args[0] and script_args[-1] == "90"
    assert call.timeout > 90


def test_recreate_defaults_to_a_generous_deadline(hops, metrics, api_key):
    metrics.respond("exec", "-i", "prometheus", stdout=http_response("200 OK", {}))
    assert hops("recreate", "sonarr/sonarr").code == 0
    assert requests(metrics)[0].docker[0][-1] == "600"


@pytest.mark.parametrize("value", ["0", "-5"])
def test_recreate_rejects_a_bad_timeout(hops, fake_host, api_key, value):
    assert hops("recreate", "sonarr/sonarr", "--timeout", value).code == 2
    assert fake_host.calls == [] and api_key == []


def test_recreate_reports_the_host_side_deadline(hops, metrics, api_key):
    metrics.respond("exec", "-i", "prometheus", returncode=143)
    outcome = hops("recreate", "sonarr/sonarr", "--timeout", "30")
    assert outcome.code == 1 and "30s" in outcome.err and "may have been interrupted" in outcome.err


def test_recreate_reports_the_client_side_deadline(hops, metrics, api_key, runner):
    fake = runner.process

    def expire(argv, *, input=None, capture=True, timeout=None):
        if input is not None:
            raise HostTimeout(f"ssh did not finish within {timeout}s")
        return fake(argv, input=input, capture=capture, timeout=timeout)

    runner.process = expire
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "may have been interrupted" in outcome.err and "sonarr/sonarr" in outcome.err
