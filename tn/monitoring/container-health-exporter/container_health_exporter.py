"""Prometheus exporter for Docker health-check status.

Uptime of a Service that is not an Exposed Service is its own health check reporting healthy
(GLOSSARY.md). Neither cAdvisor nor the Docker engine exports that, so this reads it from the
read-only docker-socket-proxy. Only containers that define a health check are reported; a
stopped one reports 0. Ad-hoc containers (`startup-order=skip`) are ignored.

It also reports containers left under Compose's temporary `<12-hex id>_<name>` name, which an
interrupted recreate leaves behind. cAdvisor keeps a container's first name, so it can't.
"""

import http.client
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOCKER = os.environ.get("DOCKER_HOST", "docker-socket-proxy:2375")
PORT = int(os.environ.get("PORT", "9714"))
TIMEOUT = float(os.environ.get("TIMEOUT", "10"))

LEFTOVER = re.compile(r"^[0-9a-f]{12}_(.+)$")

# Whether a container defines a health check, by container ID. Running containers show it in
# their status ("Up 2 hours (healthy)"); stopped ones need an inspect, done once per ID.
has_check = {}


def get(path):
    host, port = DOCKER.rsplit(":", 1)
    conn = http.client.HTTPConnection(host, int(port), timeout=TIMEOUT)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        if r.status != 200:
            raise RuntimeError(f"{path}: HTTP {r.status}")
        return json.load(r)
    finally:
        conn.close()


def health(c):
    """'healthy' | 'unhealthy' | 'starting' | 'stopped', or None without a health check."""
    status = c.get("Status") or ""
    if c.get("State") == "running":
        for state in ("unhealthy", "healthy", "starting"):
            if f"({state}" in status or f"health: {state}" in status:
                return state
        return None
    cid = c["Id"]
    if cid not in has_check:
        test = (get(f"/containers/{cid}/json").get("Config") or {}).get("Healthcheck", {}).get("Test") or []
        has_check[cid] = bool(test) and test[0] != "NONE"
    return "stopped" if has_check[cid] else None


def esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def collect():
    lines = [
        "# HELP container_health_up 1 if the Docker API answered.",
        "# TYPE container_health_up gauge",
    ]
    try:
        containers, up = get("/containers/json?all=1"), 1
    except Exception as e:  # report down, keep serving
        print(f"Docker API failed: {type(e).__name__}: {e}", flush=True)
        containers, up = [], 0
    lines.append(f"container_health_up {up}")
    lines += [
        "# HELP container_healthy 1 while the container's health check reports healthy.",
        "# TYPE container_healthy gauge",
    ]
    leftovers = []
    live = set()
    for c in containers:
        live.add(c["Id"])
        labels = c.get("Labels") or {}
        if labels.get("startup-order") == "skip":
            continue
        name = (c.get("Names") or ["/"])[0].lstrip("/")
        if LEFTOVER.match(name):
            leftovers.append({"container": name, "stack": labels.get("com.docker.compose.project", ""),
                              "service": labels.get("com.docker.compose.service", "")})
        try:
            state = health(c)
        except Exception as e:
            print(f"inspect failed: {type(e).__name__}: {e}", flush=True)
            continue
        if state is None:
            continue
        lbl = {
            "container": name,
            "stack": labels.get("com.docker.compose.project", ""),
            "tier": labels.get("tier", ""),
        }
        lines.append("container_healthy{%s} %d" % (
            ",".join(f'{k}="{esc(v)}"' for k, v in lbl.items()), state == "healthy"))
    lines += [
        "# HELP container_leftover_name 1 for a container still named <id>_<name> by an interrupted Compose recreate.",
        "# TYPE container_leftover_name gauge",
    ]
    for lbl in leftovers:
        lines.append("container_leftover_name{%s} 1" % ",".join(f'{k}="{esc(v)}"' for k, v in lbl.items()))
    for cid in set(has_check) - live:
        del has_check[cid]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = collect().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
