"""hops restart, start, stop --for and recreate: runtime state only, with the Disruptive guardrails."""
import json

import pytest

from hops import doco
from hops.errors import HopsError
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


def test_stop_silences_the_service_then_stops_it(hops, fake_host):
    fake_host.respond("exec", "-i", "alertmanager", stdout="silence-1\n")
    fake_host.respond("stop")
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h30m")
    assert outcome.code == 0
    silence, stop = fake_host.calls
    assert silence.docker[0] == PASSWORD
    args = amtool_args(silence)
    assert args[:4] == ["silence", "add", 'stack="sonarr"', 'service="sonarr"']
    assert "--duration=1h30m" in args
    assert any(a.startswith("--comment=hops stop sonarr/sonarr") for a in args)
    assert stop.docker == [["stop", "sonarr"]]
    assert "silence-1" in outcome.out
    assert "Nothing restarts" in outcome.out and "hops start sonarr/sonarr" in outcome.out


def test_stop_of_a_critical_service_with_force(hops, fake_host):
    fake_host.respond("exec", "-i", "alertmanager", stdout="silence-2\n")
    fake_host.respond("stop")
    assert hops("stop", "traefik", "--for", "10m", "--force").code == 0
    assert amtool_args(fake_host.calls[0])[:4] == ["silence", "add", 'stack="edge"', 'service="traefik"']
    assert fake_host.docker_calls[-1] == ["stop", "traefik"]


def test_stop_does_not_stop_when_the_silence_fails(hops, fake_host):
    fake_host.respond("exec", "-i", "alertmanager", returncode=1, stderr="amtool: unauthorized")
    outcome = hops("stop", "sonarr/sonarr", "--for", "1h")
    assert outcome.code == 1 and "unauthorized" in outcome.err
    assert [c for c in fake_host.docker_calls if c[0] == "stop"] == []


# recreate


def test_recreate_sends_the_api_key_on_stdin_never_in_argv(hops, fake_host, api_key, repo):
    fake_host.respond("exec", "-i", "prometheus", stdout=http_response("200 OK", {"content": "ok"}))
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 0, outcome.err
    assert api_key == [repo / "tn" / "doco-cd" / "api_secret.sops.txt"]
    (call,) = fake_host.calls
    assert all(KEY not in arg for arg in call.argv)
    assert call.docker[0][:4] == ["exec", "-i", "prometheus", "sh"]
    request = call.input
    assert request.startswith("POST /v1/api/project/sonarr/recreate?service=sonarr HTTP/1.0\r\n")
    assert f"\r\nx-api-key: {KEY}\r\n" in request and request.endswith("\r\n\r\n")
    assert "Recreated sonarr/sonarr" in outcome.out


def test_recreate_reports_doco_cds_error(hops, fake_host, api_key):
    fake_host.respond("exec", "-i", "prometheus",
                      stdout=http_response("404 Not Found", {"error": "service not found", "code": 404}))
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "404" in outcome.err and "service not found" in outcome.err


def test_recreate_fails_without_an_http_response(hops, fake_host, api_key):
    fake_host.respond("exec", "-i", "prometheus", stderr="nc: bad address 'doco-cd'")
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "bad address" in outcome.err


def test_recreate_refuses_doco_cd_itself(hops, fake_host, api_key):
    outcome = hops("recreate", "doco-cd")
    assert outcome.code == 2 and "doco-cd" in outcome.err
    assert fake_host.calls == [] and api_key == []


def test_recreate_stops_when_sops_fails(hops, fake_host, monkeypatch):
    def fail(path):
        raise HopsError("sops -d failed: no key")

    monkeypatch.setattr(doco, "decrypt", fail)
    outcome = hops("recreate", "sonarr/sonarr")
    assert outcome.code == 1 and "sops" in outcome.err
    assert fake_host.calls == []

