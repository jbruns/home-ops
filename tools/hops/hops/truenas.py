"""TrueNAS middleware: JSON-RPC 2.0 over a WebSocket (`wss://<TRUENAS_URL host>/api/current`).

The connection, login, errors and redaction live here; feature modules (e.g. hops/snapshots.py) call
methods through `Client`. Never use the REST API (`/api/v2.0`): it is deprecated and raises alerts
on the Host.

- `load_config(repo_root, env)` reads `TRUENAS_URL` and `HOPS_TRUENAS_API_KEY`. Without them the
  Host has no TrueNAS for hops, and commands that need one fail with a ConfigError.
- `Client(cfg).call(method, *params)` returns the method's result; `Client(cfg).job(...)` calls a
  method that starts a job and waits for the job's result. The client connects and logs in on first
  use; `close()` ends the connection.
- The API key travels only inside the login message: never in an argv or a log, and it is replaced
  by [REDACTED] in every error hops raises.
- `redact(data, method)` masks credential-looking fields in a method's result before it is printed.
- `pools`, `dataset_at`, `dataset_usage` and `tasks` are the Host-hygiene reads behind `hops host`.
  They build each record from named fields only, never by copying or filtering TrueNAS's objects.
"""
import datetime
import itertools
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from hops import config
from hops.errors import ConfigError, HopsError, HostTimeout

REDACTED = "[REDACTED]"
TIMEOUT = 30.0
JOB_POLL = 1.0
JOB_DONE = {"SUCCESS", "FAILED", "ABORTED"}


@dataclass(frozen=True)
class TrueNASConfig:
    url: str  # the WebSocket endpoint
    api_key: str = field(repr=False)
    verify: bool = True  # TLS certificate and host name


class Unavailable(ConfigError):
    """This host has no TrueNAS configured for hops."""


def load_config(repo_root: Path, env=None) -> TrueNASConfig:
    values = config.load_values(repo_root, env)
    missing = [k for k in ("TRUENAS_URL", "HOPS_TRUENAS_API_KEY") if not values.get(k)]
    if missing:
        raise Unavailable(f"TrueNAS is not available on this host: missing {' and '.join(missing)}; "
                          f"set it in {config.where(repo_root)}")
    url = urlsplit(values["TRUENAS_URL"])
    if url.scheme != "https" or not url.netloc:
        raise ConfigError("TRUENAS_URL must be an https:// URL, e.g. https://truenas.example")
    if "@" in url.netloc:
        raise ConfigError("TRUENAS_URL must not contain a user name or password; "
                          "hops logs in with HOPS_TRUENAS_API_KEY")
    return TrueNASConfig(url=f"wss://{url.netloc}/api/current", api_key=values["HOPS_TRUENAS_API_KEY"],
                         verify=values.get("HOPS_TRUENAS_INSECURE", "") not in ("1", "true", "yes"))


class Transport(Protocol):
    def send(self, text: str) -> None: ...
    def recv(self) -> str: ...
    def close(self) -> None: ...


Connect = Callable[..., Transport]  # connect(url, *, verify: bool, timeout: float) -> Transport


class TrueNASError(HopsError):
    """A TrueNAS method returned an error."""

    def __init__(self, message: str, errname: str | None = None):
        super().__init__(message)
        self.errname = errname  # e.g. EACCES, ENOENT


class Client:
    def __init__(self, cfg: TrueNASConfig, *, connect: Connect | None = None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = TIMEOUT):
        self.cfg = cfg
        self._connect = connect or websocket_connect
        self._sleep = sleep
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._ws: Transport | None = None

    def close(self) -> None:
        if self._ws is not None:
            ws, self._ws = self._ws, None
            ws.close()

    def call(self, method: str, *params):
        if self._ws is None:
            self._open()
        return self._rpc(method, list(params))

    def job(self, method: str, *params, timeout: float | None = None):
        """Call a method that starts a job; wait for it and return the job's result."""
        job_id = self.call(method, *params)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            job = self.call("core.get_jobs", [["id", "=", job_id]], {"get": True})
            if job.get("state") == "SUCCESS":
                return job.get("result")
            if job.get("state") in JOB_DONE:
                raise TrueNASError(self._scrub(f"{method}: job {job_id} {job['state'].lower()}: "
                                               f"{job.get('error') or 'no error message'}"))
            if deadline is not None and time.monotonic() >= deadline:
                raise HostTimeout(f"{method}: job {job_id} still {job.get('state')} after {timeout:g}s")
            self._sleep(JOB_POLL)

    def _open(self) -> None:
        try:
            self._ws = self._connect(self.cfg.url, verify=self.cfg.verify, timeout=self._timeout)
        except HopsError as exc:
            raise type(exc)(self._scrub(str(exc))) from None
        if self._rpc("auth.login_with_api_key", [self.cfg.api_key]) is not True:
            self.close()
            raise HopsError(self._scrub(f"TrueNAS at {self.cfg.url} rejected HOPS_TRUENAS_API_KEY"))

    def _rpc(self, method: str, params: list):
        rid = next(self._ids)
        try:
            self._ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
            while True:  # skip notifications and anything else not answering this request
                msg = json.loads(self._ws.recv())
                if isinstance(msg, dict) and msg.get("id") == rid:
                    break
        except HopsError as exc:
            self.close()
            raise type(exc)(self._scrub(f"{method}: {exc}")) from None
        except ValueError:
            self.close()
            raise HopsError(f"TrueNAS {method}: the answer is not JSON") from None
        if "error" in msg:
            raise self._error(method, msg["error"])
        return msg.get("result")

    def _error(self, method: str, error: dict) -> TrueNASError:
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        reason = data.get("reason") or error.get("message") or "unknown error"
        errname = data.get("errname")
        text = f"TrueNAS {method}: {errname + ': ' if errname else ''}{reason}".rstrip()
        return TrueNASError(self._scrub(text), errname)

    def _scrub(self, text: str) -> str:
        return text.replace(self.cfg.api_key, REDACTED) if self.cfg.api_key else text


def websocket_connect(url: str, *, verify: bool, timeout: float) -> Transport:
    """The real transport (websocket-client); its errors become HopsError or HostTimeout."""
    try:
        import websocket
    except ImportError:
        raise ConfigError("hops needs websocket-client for TrueNAS: reinstall with "
                          "`uv tool install -e tools/hops`, or `pip install websocket-client`") from None
    import ssl

    class _WebSocket:
        def __init__(self):
            sslopt = {} if verify else {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
            self.ws = self._guard(lambda: websocket.create_connection(url, timeout=timeout, sslopt=sslopt))

        @staticmethod
        def _guard(fn):
            try:
                return fn()
            except websocket.WebSocketTimeoutException:
                raise HostTimeout(f"no answer from TrueNAS at {url} within {timeout:g}s") from None
            except (websocket.WebSocketException, OSError) as exc:
                raise HopsError(f"cannot reach TrueNAS at {url}: {exc}") from None

        def send(self, text: str) -> None:
            self._guard(lambda: self.ws.send(text))

        def recv(self) -> str:
            return self._guard(self.ws.recv)

        def close(self) -> None:
            try:
                self.ws.close()
            except (websocket.WebSocketException, OSError):
                pass

    return _WebSocket()


# A field is a credential when its name contains one of these (case-insensitive).
SECRET_NAMES = ("credential", "key", "pass", "pwd", "bindpw", "secret", "token", "salt", "hash",
                "community")
# Fields (exact names, from TrueNAS's API schema) holding user-written scripts, commands, command
# arguments or auxiliary config, which can hold a secret in any form. Masked when a string or list.
FREE_TEXT = {
    "command", "script", "pre_script", "post_script",  # cronjob, initshutdownscript, cloudsync, acme
    "args", "extra",  # cloudsync rclone arguments, rsynctask rsync arguments
    "shutdowncmd", "options", "optionsupsd",  # ups (also ssh, snmp, ftp `options`)
    "sudo_commands", "sudo_commands_nopasswd",  # user, group
    "smb_options", "auxsmbconf", "cmdline",  # smb, sharing.smb, process command lines
}
# A task's embedded `job` keeps only its status. Its logs, error, result and arguments can echo
# whatever a script printed, credentials included; the other keys are dropped.
JOB_FIELDS = ("id", "method", "state", "time_started", "time_finished")


def job_status(job: dict) -> dict:
    out = {k: job[k] for k in JOB_FIELDS if k in job}
    if isinstance(job.get("progress"), dict) and "percent" in job["progress"]:
        out["progress"] = {"percent": job["progress"]["percent"]}
    return out


# A URL's user name and password, e.g. http://user:pass@proxy:3128, in any string.
URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s?#]*@")
# Fields holding a cloud credential's settings (cloudsync.credentials.*): all but `type` may be secret.
PROVIDER_FIELDS = ("attributes", "provider")


def redact(data, method: str = ""):
    """A copy of `method`'s result with credential-looking values, user-written scripts and commands,
    and URL userinfo in any string, replaced by [REDACTED], and embedded jobs reduced to their status."""
    provider = method.startswith("cloudsync.credentials.")
    secret = SECRET_NAMES + (("file",) if method.startswith("kerberos.keytab.") else ())

    def walk(x):
        if isinstance(x, str):
            return URL_USERINFO.sub(rf"\1{REDACTED}@", x)
        if isinstance(x, list):
            return [walk(v) for v in x]
        if not isinstance(x, dict):
            return x
        out = {}
        for k, v in x.items():
            name = str(k).lower()
            if name == "job" and isinstance(v, dict):
                out[k] = walk(job_status(v))
            elif v is not None and any(s in name for s in secret):
                out[k] = REDACTED
            elif name in FREE_TEXT and isinstance(v, (str, list)):
                out[k] = REDACTED
            elif provider and name in PROVIDER_FIELDS and isinstance(v, dict):
                out[k] = {pk: pv if pk == "type" else REDACTED for pk, pv in v.items()}
            else:
                out[k] = walk(v)
        return out

    return walk(data)


# Host-hygiene reads (`hops host`). Task objects carry credentials, scripts, rclone arguments and job
# logs that can echo secrets, so every record here is built from an allowlist of named fields.
DATASET_PROPERTIES = ["mountpoint", "used", "available", "usedbysnapshots", "quota"]


def timestamp(value) -> str | None:
    """TrueNAS's `{"$date": <ms>}` as ISO 8601 UTC, e.g. 2026-10-23T06:00:00Z."""
    if not isinstance(value, dict) or not isinstance(value.get("$date"), (int, float)):
        return None
    when = datetime.datetime.fromtimestamp(value["$date"] / 1000, datetime.UTC)
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def pools(client: Client) -> list[dict]:
    out = []
    for p in client.call("pool.query", [], {"order_by": ["name"]}):
        size, allocated = p.get("size"), p.get("allocated")
        out.append({
            "name": p.get("name"), "status": p.get("status"), "healthy": p.get("healthy"),
            "size_bytes": size, "allocated_bytes": allocated, "free_bytes": p.get("free"),
            "used_percent": round(100 * allocated / size, 1) if size and allocated is not None else None,
        })
    return out


def dataset_at(client: Client, path: str) -> str | None:
    """The ZFS dataset a path on the Host lives on, or None when it is not on ZFS."""
    fs = client.call("filesystem.statfs", path)
    return fs.get("source") if fs.get("fstype") == "zfs" else None


def dataset_usage(client: Client, name: str) -> dict:
    """One dataset's space. Unlike pool.dataset.query, this also finds hidden ones (ix-apps)."""
    found = client.call("zfs.resource.query", {"paths": [name], "properties": DATASET_PROPERTIES})
    props = (found[0].get("properties") or {}) if found else {}

    def value(prop):
        v = (props.get(prop) or {}).get("value")
        return v if isinstance(v, int) else None

    return {
        "name": name, "mountpoint": (props.get("mountpoint") or {}).get("value"),
        "used_bytes": value("used"), "available_bytes": value("available"),
        "snapshots_bytes": value("usedbysnapshots"), "quota_bytes": value("quota") or None,
    }


def _last_run(state: str | None, started: str | None, finished: str | None) -> dict | None:
    if state is None:
        return None
    return {"state": state, "started_at": started, "finished_at": finished}


def tasks(client: Client) -> list[dict]:
    """Periodic snapshot and Cloud Sync tasks: what each covers and its last run's state and times."""
    out = []
    for t in client.call("pool.snapshottask.query", [], {"order_by": ["id"]}):
        st = t.get("state") if isinstance(t.get("state"), dict) else {}
        when = timestamp(st.get("datetime"))
        running = st.get("state") == "RUNNING"
        out.append({
            "kind": "snapshot", "id": t.get("id"), "name": t.get("naming_schema"), "source": t.get("dataset"),
            "enabled": t.get("enabled"),
            "last_run": _last_run(st.get("state"), when if running else None, None if running else when),
        })
    for t in client.call("cloudsync.query", [], {"order_by": ["id"]}):
        job = job_status(t["job"]) if isinstance(t.get("job"), dict) else {}
        out.append({
            "kind": "cloudsync", "id": t.get("id"), "name": t.get("description"), "source": t.get("path"),
            "enabled": t.get("enabled"),
            "last_run": _last_run(job.get("state"), timestamp(job.get("time_started")),
                                  timestamp(job.get("time_finished"))),
        })
    return out
