"""amtool inside the alertmanager container, authenticated without the password leaving the Host.

Alertmanager requires basic auth (its web.sops.yml). Prometheus holds the shared `prometheus`
password as a Compose secret (prometheus.yml `alerting:`), so the Host pipes that file from the
prometheus container into the alertmanager container, where sh writes amtool's HTTP config to
amtool's stdin. The password is never in a process argument or on this machine.
"""
from hops import docker
from hops.runner import Result, Runner

USERNAME = "prometheus"
PASSWORD_FILE = "/run/secrets/prometheus_password"
URL = "http://127.0.0.1:9093"
# `read` and `printf` are sh builtins, and sed gets the password on stdin; YAML single quotes
# escape ' as ''. "$@" holds amtool's arguments.
SCRIPT = (
    "IFS= read -r p; p=$(printf %s \"$p\" | sed \"s/'/''/g\"); "
    f"printf \"basic_auth:\\n  username: {USERNAME}\\n  password: '%s'\\n\" \"$p\" | "
    f"amtool --alertmanager.url={URL} --http.config.file=/dev/stdin \"$@\""
)


def amtool(runner: Runner, declared, *args: str, check: bool = True) -> Result:
    """Run `amtool <args>` against Alertmanager; returns its captured output."""
    prometheus = docker.container_ref(runner, declared.resolve_one("monitoring/prometheus"))
    alertmanager = docker.container_ref(runner, declared.resolve_one("monitoring/alertmanager"))
    return runner.pipeline(
        ["exec", prometheus, "cat", PASSWORD_FILE],
        ["exec", "-i", alertmanager, "sh", "-c", SCRIPT, "amtool", *args],
        check=check,
    )
