"""The Runner: the one place that builds the ssh argv for `sudo -n docker` on the Host."""
import pytest

from hops.errors import HopsError

SSH = ["ssh", "-i", "/keys/id", "-o", "BatchMode=yes", "-o", "ControlMaster=auto"]


def ssh_options(control_dir):
    return [*SSH, "-o", f"ControlPath={control_dir}/%C", "-o", "ControlPersist=10m"]


def test_docker_runs_over_ssh_with_a_shared_connection(runner, fake_host, host_config):
    fake_host.respond("ps", stdout="abc\n")
    result = runner.docker("ps", "-aq", "--filter", "label=a b")
    assert result.stdout == "abc\n"
    assert fake_host.calls[0].argv == [
        *ssh_options(host_config.control_dir), "-T", "agent@tn.example",
        "sudo -n docker ps -aq --filter 'label=a b'",
    ]
    assert fake_host.calls[0].capture
    assert host_config.control_dir.is_dir()


def test_pipeline_runs_each_docker_command_on_the_host(runner, fake_host):
    fake_host.respond("exec", "-i", "b")
    runner.pipeline(["exec", "a", "cat", "/x"], ["exec", "-i", "b", "sh", "-c", "read -r p; echo $p"], input="hi")
    call = fake_host.calls[0]
    assert call.remote == "sudo -n docker exec a cat /x | sudo -n docker exec -i b sh -c 'read -r p; echo $p'"
    assert call.input == "hi"


def test_attach_passes_stdio_through_and_uses_a_tty_on_request(runner, fake_host, host_config):
    fake_host.respond("logs")
    assert runner.attach("logs", "-f", "x", tty=True) == 0
    call = fake_host.calls[0]
    assert call.argv == [*ssh_options(host_config.control_dir), "-t", "agent@tn.example", "sudo -n docker logs -f x"]
    assert not call.capture


def test_failed_command_raises_with_its_stderr(runner, fake_host):
    fake_host.respond("inspect", returncode=1, stderr="Error: No such object: nope\n")
    with pytest.raises(HopsError, match="docker inspect nope failed: Error: No such object: nope"):
        runner.docker("inspect", "nope")
    assert runner.docker("inspect", "nope", check=False).returncode == 1


def test_unreachable_host_is_reported_as_ssh(runner, fake_host):
    fake_host.respond("ps", returncode=255, stderr="ssh: connect to host tn.example port 22: Connection refused\n")
    with pytest.raises(HopsError, match="ssh agent@tn.example failed: ssh: connect to host"):
        runner.docker("ps", check=False)



PIPE_SCRIPT = """
import sys
from hops import cli
from hops.commands import status

def flood(args, ctx):
    for i in range(100000):
        ctx.stdout.write(f"line {i}\\n")
    return 0

status.run = flood
sys.exit(cli.main(["status"], ctx=cli.Context(repo_root=".")))
"""


def test_hops_exits_0_when_its_reader_stops_early():
    # `hops status | head`: the reader closes the pipe; that is not a hops failure.
    import subprocess
    import sys
    from pathlib import Path

    proc = subprocess.Popen([sys.executable, "-c", PIPE_SCRIPT], cwd=Path(__file__).resolve().parents[1],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdout.readline()
    proc.stdout.close()
    assert proc.wait(timeout=30) == 0
    assert proc.stderr.read() == b""
