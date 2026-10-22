"""hops logs <svc>: docker logs on the Host, with --since, --tail and -f passed through."""
import json


def test_logs_streams_to_the_terminal_with_options_passed_through(hops, fake_host, host_config):
    fake_host.respond("logs")
    assert hops("logs", "sonarr/sonarr", "--since", "1h", "--tail", "50").code == 0
    call = fake_host.calls[0]
    assert call.docker == [["logs", "--since", "1h", "--tail", "50", "sonarr"]]
    assert not call.capture
    assert "-T" in call.argv


def test_logs_follow_uses_a_tty(hops, fake_host):
    fake_host.respond("logs")
    hops("logs", "traefik", "-f")
    call = fake_host.calls[0]
    assert call.docker == [["logs", "--follow", "traefik"]]
    assert "-t" in call.argv and "-T" not in call.argv


def test_logs_returns_the_remote_exit_code(hops, fake_host):
    fake_host.respond("logs", returncode=1)
    assert hops("logs", "traefik").code == 1


def test_logs_json_merges_stdout_and_stderr_by_time(hops, fake_host):
    fake_host.respond("logs", stdout="2026-10-21T21:00:00.000000001Z one\n2026-10-21T21:00:00.000000003Z three\n",
                      stderr="2026-10-21T21:00:00.000000002Z two: oops\n")
    outcome = hops("logs", "traefik", "--tail", "3", "--json")
    assert fake_host.calls[0].docker == [["logs", "--tail", "3", "--timestamps", "traefik"]]
    assert fake_host.calls[0].capture
    assert json.loads(outcome.out) == {
        "stack": "edge", "service": "traefik", "container": "traefik",
        "lines": [
            {"time": "2026-10-21T21:00:00.000000001Z", "stream": "stdout", "text": "one"},
            {"time": "2026-10-21T21:00:00.000000002Z", "stream": "stderr", "text": "two: oops"},
            {"time": "2026-10-21T21:00:00.000000003Z", "stream": "stdout", "text": "three"},
        ],
    }


def test_logs_json_cannot_follow(hops, fake_host):
    outcome = hops("logs", "traefik", "-f", "--json")
    assert outcome.code == 2
    assert "--json" in outcome.err
    assert fake_host.calls == []


def test_logs_of_a_service_without_container_name_looks_it_up(hops, fake_host):
    from hops_fakes import container

    doco = container("doco-cd-doco-cd-1", project="doco-cd", service="doco-cd")
    fake_host.respond("ps", stdout=doco["Id"] + "\n")
    fake_host.respond("inspect", stdout=json.dumps([doco]))
    fake_host.respond("logs")
    hops("logs", "doco-cd")
    assert fake_host.docker_calls[-1] == ["logs", "doco-cd-doco-cd-1"]
