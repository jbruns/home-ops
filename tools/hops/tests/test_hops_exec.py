"""hops exec <svc> [-- cmd…]: a command in a Service's container, or an interactive shell."""
import pytest


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr("hops.terminal.stdin_is_tty", lambda: True)


def test_exec_runs_a_command_non_interactively_and_returns_its_exit_code(hops, fake_host):
    fake_host.respond("exec", returncode=3)
    outcome = hops("exec", "sonarr/sonarr", "--", "ls", "-la", "/config dir")
    assert outcome.code == 3
    call = fake_host.calls[0]
    assert call.docker == [["exec", "sonarr", "ls", "-la", "/config dir"]]
    assert not call.capture
    assert "-T" in call.argv


def test_exec_needs_no_separator_before_the_command(hops, fake_host):
    fake_host.respond("exec")
    hops("exec", "traefik", "traefik", "version")
    assert fake_host.docker_calls == [["exec", "traefik", "traefik", "version"]]


def test_exec_without_a_command_refuses_without_a_tty(hops, fake_host):
    outcome = hops("exec", "traefik")
    assert outcome.code == 2
    assert "TTY" in outcome.err
    assert fake_host.calls == []


def test_exec_without_a_command_opens_a_shell_over_ssh_t(hops, fake_host, tty):
    fake_host.respond("exec")
    assert hops("exec", "traefik").code == 0
    call = fake_host.calls[0]
    exec_args = call.docker[0]
    assert exec_args[:3] == ["exec", "-it", "traefik"]
    assert exec_args[3:5] == ["sh", "-c"]
    assert "-t" in call.argv and "-T" not in call.argv
    assert not call.capture


def test_exec_of_an_unknown_service_fails_before_the_host(hops, fake_host):
    outcome = hops("exec", "nope", "--", "true")
    assert outcome.code == 2
    assert fake_host.calls == []
