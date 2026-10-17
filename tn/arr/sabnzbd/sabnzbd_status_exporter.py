"""Prometheus exporter for SABnzbd's Usenet provider (server) health.

exportarr covers the queue, quota and disk, but not each server's state. `api?mode=status`
reports, per server, whether it is enabled and the error or warning SABnzbd last hit on it
(e.g. failed authentication, too many connections). The API is read once per scrape.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

URL = os.environ.get("SABNZBD_URL", "http://sabnzbd:8080").rstrip("/") + "/api?" + urllib.parse.urlencode(
    {"mode": "status", "output": "json", "apikey": os.environ["SABNZBD_API_KEY"]}
)
PORT = int(os.environ.get("PORT", "9712"))
TIMEOUT = float(os.environ.get("TIMEOUT", "10"))

HELP = {
    "sabnzbd_status_up": "1 if the status API answered.",
    "sabnzbd_status_scrape_duration_seconds": "Time taken to read the status API.",
    "sabnzbd_usenet_server_active": "1 if the server is enabled in SABnzbd.",
    "sabnzbd_usenet_server_optional": "1 if the server is optional (a failure does not fail the download).",
    "sabnzbd_usenet_server_priority": "Server priority; 0 is tried first.",
    "sabnzbd_usenet_server_connections": "Configured connections.",
    "sabnzbd_usenet_server_error": "1 while SABnzbd reports an error for the server.",
    "sabnzbd_usenet_server_error_info": "The server's current error, as a label.",
    "sabnzbd_usenet_server_warning": "1 while SABnzbd reports a warning for the server.",
    "sabnzbd_usenet_server_warning_info": "The server's current warning, as a label.",
}


def esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def fetch():
    with urllib.request.urlopen(URL, timeout=TIMEOUT) as r:
        return json.load(r)["status"]


def collect():
    samples = []
    start = time.monotonic()
    try:
        servers = fetch().get("servers") or []
        up = 1
    except Exception as e:  # report down, keep serving
        print(f"status API failed: {type(e).__name__}", flush=True)
        servers, up = [], 0
    samples.append(("sabnzbd_status_up", {}, up))
    samples.append(("sabnzbd_status_scrape_duration_seconds", {}, round(time.monotonic() - start, 3)))

    for s in servers:
        name = {"server": s.get("servername", "")}
        samples.append(("sabnzbd_usenet_server_active", name, int(bool(s.get("serveractive")))))
        samples.append(("sabnzbd_usenet_server_optional", name, int(bool(s.get("serveroptional")))))
        samples.append(("sabnzbd_usenet_server_priority", name, s.get("serverpriority") or 0))
        samples.append(("sabnzbd_usenet_server_connections", name, s.get("servertotalconn") or 0))
        for kind in ("error", "warning"):
            text = (s.get(f"server{kind}") or "").strip()
            samples.append((f"sabnzbd_usenet_server_{kind}", name, int(bool(text))))
            if text:
                samples.append((f"sabnzbd_usenet_server_{kind}_info", {**name, kind: text[:200]}, 1))

    out, seen = [], set()
    for metric, labels, value in samples:
        if metric not in seen:
            seen.add(metric)
            out += [f"# HELP {metric} {HELP[metric]}", f"# TYPE {metric} gauge"]
        lbl = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
        out.append(f"{metric}{{{lbl}}} {value}" if lbl else f"{metric} {value}")
    return "\n".join(out) + "\n"


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
