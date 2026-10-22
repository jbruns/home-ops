"""The Runner: the one place that builds the ssh argv for `sudo -n docker` on the Host."""
import os
import shlex
import subprocess

import pytest

from hops.errors import HopsError
from hops.runner import Result

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


def test_pipeline_runs_each_docker_command_on_the_host_under_bash_pipefail(runner, fake_host):
    fake_host.respond("exec", "-i", "b")
    runner.pipeline(["exec", "a", "cat", "/x"], ["exec", "-i", "b", "sh", "-c", "read -r p; echo $p"], input="hi")
    call = fake_host.calls[0]
    # The Host's /bin/sh is dash, which has no pipefail; bash is named explicitly.
    assert shlex.split(call.remote) == [
        "bash", "-c",
        "set -o pipefail; sudo -n docker exec a cat /x | sudo -n docker exec -i b sh -c 'read -r p; echo $p'",
    ]
    assert call.docker == [["exec", "a", "cat", "/x"], ["exec", "-i", "b", "sh", "-c", "read -r p; echo $p"]]
    assert call.input == "hi"


def test_pipeline_fails_when_the_first_command_fails_and_the_last_succeeds(runner, tmp_path):
    """Run the remote command line locally, with sudo and docker stubs: the producer fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sudo").write_text('#!/bin/sh\nshift\nexec "$@"\n')
    (bin_dir / "docker").write_text(
        '#!/bin/sh\nif [ "$1" = produce ]; then echo "no such secret" >&2; exit 1; fi\ncat >/dev/null\n')
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    def local_host(argv, *, input=None, capture=True):
        done = subprocess.run(["bash", "-c", argv[-1]], input=input, capture_output=True, text=True,
                              env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
        return Result(argv, done.returncode, done.stdout, done.stderr)

    runner.process = local_host
    with pytest.raises(HopsError, match="no such secret"):
        runner.pipeline(["produce"], ["consume"])
    assert runner.pipeline(["produce"], ["consume"], check=False).returncode == 1


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


def test_a_deadline_ends_the_ssh_process_and_raises(tmp_path):
    from hops.errors import HostTimeout
    from hops.runner import run_process

    with pytest.raises(HostTimeout, match="0.2s"):
        run_process(["sleep", "5"], timeout=0.2)


def test_docker_passes_its_deadline_to_the_ssh_process(runner, fake_host):
    fake_host.respond("ps")
    runner.docker("ps", timeout=12)
    runner.docker("ps")
    assert [c.timeout for c in fake_host.calls] == [12, None]
