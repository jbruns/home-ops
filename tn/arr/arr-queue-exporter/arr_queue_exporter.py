"""Prometheus exporter for Sonarr/Radarr queue items that are stuck.

exportarr's `*_queue_total` cannot be used: it emits one sample, labelled with only the last
item's state. This reads `/api/v3/queue` (including items the app could not match) and
reports each item that needs attention, so an alert's `for` measures how long that item has
been stuck regardless of which stuck state it is in.
"""

import json
import os
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APPS = {
    "sonarr": (os.environ.get("SONARR_URL", "http://sonarr:8989"), os.environ["SONARR_API_KEY"],
               "includeUnknownSeriesItems"),
    "radarr": (os.environ.get("RADARR_URL", "http://radarr:7878"), os.environ["RADARR_API_KEY"],
               "includeUnknownMovieItems"),
}
PORT = int(os.environ.get("PORT", "9713"))
TIMEOUT = float(os.environ.get("TIMEOUT", "10"))

# Downloaded but not imported, or failed and waiting for the app to act (trackedDownloadState),
# or any item the app flags (trackedDownloadStatus). importPending is normal briefly.
STUCK_STATES = {"importPending", "importBlocked", "failedPending"}
STUCK_STATUSES = {"warning", "error"}

HELP = {
    "arr_queue_up": "1 if the app's queue API answered.",
    "arr_queue_items": "Queue items by tracked download state and status.",
    "arr_queue_item_stuck": "1 for each queue item that needs attention.",
    "arr_queue_item_info": "A stuck item's state, status and first message, as labels.",
}


def esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def fetch(url, key, unknown):
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/v3/queue?pageSize=1000&{unknown}=true", headers={"X-Api-Key": key}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r).get("records") or []


def message(r):
    for m in r.get("statusMessages") or []:
        for text in m.get("messages") or []:
            return text
    return r.get("errorMessage") or ""


def collect():
    samples = []
    for app, (url, key, unknown) in APPS.items():
        try:
            records, up = fetch(url, key, unknown), 1
        except Exception as e:  # report down, keep serving
            print(f"{app} queue API failed: {type(e).__name__}", flush=True)
            records, up = [], 0
        samples.append(("arr_queue_up", {"app": app}, up))
        counts = Counter((r.get("trackedDownloadState", ""), r.get("trackedDownloadStatus", "")) for r in records)
        for (state, status), n in sorted(counts.items()):
            samples.append(("arr_queue_items", {"app": app, "state": state, "status": status}, n))
        for r in records:
            state, status = r.get("trackedDownloadState", ""), r.get("trackedDownloadStatus", "")
            if state not in STUCK_STATES and status not in STUCK_STATUSES:
                continue
            item = {"app": app, "id": r.get("id", ""), "title": (r.get("title") or "")[:200]}
            samples.append(("arr_queue_item_stuck", item, 1))
            info = {**item, "state": state, "status": status, "message": message(r)[:200]}
            samples.append(("arr_queue_item_info", info, 1))

    out, seen = [], set()
    for metric, labels, value in samples:
        if metric not in seen:
            seen.add(metric)
            out += [f"# HELP {metric} {HELP[metric]}", f"# TYPE {metric} gauge"]
        lbl = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
        out.append(f"{metric}{{{lbl}}} {value}")
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
