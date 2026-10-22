"""doco-cd's REST API, called from inside the prometheus container with a key decrypted on this machine.

The API is reachable only on doco-cd's networks; prometheus shares `metrics` with it and has
busybox nc. The whole HTTP request, x-api-key included, goes over ssh's stdin, so the key is never in
a process argument on either machine and never written to disk on the Host. `nc -e` hands the
socket to sh, which copies the request from stdin and then the response to stdout. A plain
`… | nc` would half-close the connection at the end of stdin, and doco-cd cancels a recreate whose
client has gone away.
"""
import json
import subprocess
from pathlib import Path
from urllib.parse import quote

from hops import docker
from hops.declared import Declared, Service
from hops.errors import ConfigError, HopsError
from hops.runner import Runner

DOCO_CD = "doco-cd/doco-cd"
API_SECRET = Path("doco-cd") / "api_secret.sops.txt"  # under the Stack root
ADDRESS = ("doco-cd", 80)
# fd 5 keeps ssh's stdin and fd 6 its stdout (nc uses fd 3 for the socket) across nc's exec, which makes the socket stdin and stdout.
SCRIPT = (f"exec 5<&0 6>&1; exec nc -w 10 {ADDRESS[0]} {ADDRESS[1]} "
          "-e sh -c 'cat <&5 && exec cat >&6 5<&-'")


def decrypt(path: Path) -> str:
    """`sops -d path`, run on this machine."""
    try:
        done = subprocess.run(["sops", "-d", str(path)], capture_output=True, text=True)
    except FileNotFoundError:
        raise ConfigError("sops is not installed; hops recreate decrypts doco-cd's API key with it") from None
    if done.returncode != 0:
        raise HopsError(f"sops -d {path} failed: {done.stderr.strip()}")
    return done.stdout


def api_key(repo_root: Path, stack_root: str) -> str:
    key = decrypt(repo_root / stack_root / API_SECRET).strip()
    if not key or "\n" in key or "\r" in key:
        raise HopsError(f"{stack_root}/{API_SECRET} does not hold a single-line API key")
    return key


def request(service: Service, key: str) -> str:
    path = f"/v1/api/project/{quote(service.stack, safe='')}/recreate?service={quote(service.name, safe='')}"
    # HTTP/1.0: the response is never chunked, and doco-cd closes the connection after it.
    return f"POST {path} HTTP/1.0\r\nHost: {ADDRESS[0]}\r\nx-api-key: {key}\r\nContent-Length: 0\r\n\r\n"


def recreate(runner: Runner, declared: Declared, service: Service, key: str) -> None:
    """Ask doco-cd to force-recreate one Service; raises HopsError unless it answers 2xx."""
    prometheus = docker.container_ref(runner, declared.resolve_one("monitoring/prometheus"))
    result = runner.docker("exec", "-i", prometheus, "sh", "-c", SCRIPT, input=request(service, key))
    # The Runner reads text with universal newlines, so CRLF arrives as LF.
    head, _, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
    status = head.split("\n", 1)[0].split(" ", 2)
    if len(status) < 2 or not status[0].startswith("HTTP/") or not status[1].isdigit():
        detail = result.stderr.strip() or result.stdout.strip() or "empty response"
        raise HopsError(f"no HTTP response from doco-cd: {detail}")
    code = int(status[1])
    if not 200 <= code < 300:
        try:
            data = json.loads(body)
            message = " ".join(str(data[k]) for k in ("error", "details") if data.get(k))
        except (json.JSONDecodeError, AttributeError):
            message = body.strip()
        raise HopsError(f"doco-cd answered {' '.join(status[1:])} for {service.key}: {message or 'no detail'}")
