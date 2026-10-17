"""Backup coverage: is every volume stack-back should back up present in the newest snapshot?

Expected paths come from Docker, not from stack-back's run: every container (stopped ones too)
labelled for stack-back, filtered and mapped exactly as stack-back v1.5.4 does
(containers.py filter_mounts / get_volume_backup_destination, containers_db.py
backup_destination_path), with EXCLUDE_BIND_MOUNTS and INCLUDE_ALL_COMPOSE_PROJECTS on.

A path is "due" once it should be in a backup: a volume whose container and volume both
predate the newest /volumes snapshot, or a database whose container is older than 26h. A
recreate defers the check to the next backup. A volume's live size (Docker's disk usage) tells
an empty snapshot dir that is expected (the volume is empty) from one that is not.
"""

import http.client
import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TRUE = {"1", "true", "True", "TRUE"}
DB_LABELS = ("stack-back.mariadb", "stack-back.mysql", "stack-back.postgres")
DB_MOUNTS = {"/var/lib/mysql", "/var/lib/mariadb", "/var/lib/postgresql/data"}
DB_DUE_AFTER = 26 * 3600
INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "3600"))
PORT = int(os.environ.get("PORT", "8002"))

metrics = ""


class DockerConnection(http.client.HTTPConnection):
    def __init__(self, timeout=30):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/docker.sock")


def docker(path, timeout=30):
    conn = DockerConnection(timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"docker GET {path}: HTTP {resp.status}")
        return json.loads(body)
    finally:
        conn.close()


def restic(*args):
    out = subprocess.run(
        ["restic", "--no-lock", "--json", *args],
        check=True, capture_output=True, text=True, timeout=1800,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.startswith("{") or line.startswith("[")]


def ts(rfc3339):
    return datetime.fromisoformat(rfc3339).timestamp()


def patterns(value):
    value = (value or "").strip()
    return value.split(",") if value else None


def volume_sizes():
    """{volume name: live bytes}; -1 where Docker has no figure."""
    # Docker walks every volume to size it: ~20s here, so a longer timeout.
    vols = docker("/system/df?type=volume", timeout=300).get("Volumes") or []
    return {v["Name"]: (v.get("UsageData") or {}).get("Size", -1) for v in vols}


def expected_paths():
    """[(kind, project, service, path, since, volume)] for every stack-back labelled container."""
    volume_created = {v["Name"]: ts(v["CreatedAt"]) for v in docker("/volumes")["Volumes"] or []}
    paths = []
    for c in docker("/containers/json?all=1"):
        labels = c.get("Labels") or {}
        if "stack-back" in c.get("Image", "") or labels.get("com.docker.compose.oneoff") == "True":
            continue
        project = labels.get("com.docker.compose.project", "")
        service = labels.get("com.docker.compose.service", "")
        base = f"/{project}/{service}" if project else f"/{service}"
        created = float(c["Created"])
        is_db = any(labels.get(l) in TRUE for l in DB_LABELS)

        if is_db:
            paths.append(("database", project, service, f"/databases{base}/all_databases.sql", created, None))

        if labels.get("stack-back.volumes") not in TRUE:
            continue
        include = patterns(labels.get("stack-back.volumes.include"))
        exclude = patterns(labels.get("stack-back.volumes.exclude"))
        for m in c.get("Mounts") or []:
            if m.get("Type") != "volume":
                continue
            src = m.get("Source", "")
            if include:
                if not any(p in src for p in include):
                    continue
            elif exclude:
                if any(p in src for p in exclude):
                    continue
            elif is_db and m["Destination"] in DB_MOUNTS:
                continue
            since = max(created, volume_created.get(m.get("Name"), created))
            paths.append(("volume", project, service, f"/volumes{base}{m['Destination']}", since, m.get("Name")))
    return paths


def collect():
    now = time.time()
    expected = expected_paths()
    sizes = volume_sizes()
    snapshots = restic("snapshots")[0] or []

    volume_snaps = [s for s in snapshots if s["paths"] == ["/volumes"]]
    newest = max(volume_snaps, key=lambda s: ts(s["time"]), default=None)
    newest_ts = ts(newest["time"]) if newest else 0

    db_newest = {}
    for s in snapshots:
        if len(s["paths"]) == 1 and s["paths"][0].startswith("/databases/"):
            p = s["paths"][0]
            db_newest[p] = max(db_newest.get(p, 0), ts(s["time"]))

    # One non-recursive listing of every expected volume dir: the dir itself plus its children.
    vol_paths = sorted({p for k, _, _, p, _, _ in expected if k == "volume"})
    present, entries = set(), {}
    if newest and vol_paths:
        for node in restic("ls", newest["id"], *vol_paths):
            path = node.get("path")
            if node.get("struct_type") != "node" or not path:
                continue
            if path in vol_paths:
                present.add(path)
            parent = path.rsplit("/", 1)[0]
            if parent in vol_paths:
                entries[parent] = entries.get(parent, 0) + 1

    lines = [
        "# HELP backup_coverage_expected Path stack-back is configured to back up.",
        "# TYPE backup_coverage_expected gauge",
        "# HELP backup_coverage_due 1 once the path should be in a backup (see backup_coverage.py).",
        "# TYPE backup_coverage_due gauge",
        "# HELP backup_coverage_present 1 if the path is in its newest snapshot.",
        "# TYPE backup_coverage_present gauge",
        "# HELP backup_coverage_entries Direct children of a volume path in the newest /volumes snapshot.",
        "# TYPE backup_coverage_entries gauge",
        "# HELP backup_coverage_volume_bytes Live size of the volume now (-1 unknown); 0 means an empty snapshot dir is expected.",
        "# TYPE backup_coverage_volume_bytes gauge",
        "# HELP backup_coverage_snapshot_timestamp Newest snapshot holding the path (volumes: newest /volumes snapshot).",
        "# TYPE backup_coverage_snapshot_timestamp gauge",
    ]
    for kind, project, service, path, since, volume in expected:
        lbl = f'kind="{kind}",project="{project}",service="{service}",path="{path}"'
        if kind == "volume":
            due = since < newest_ts
            is_present = path in present
            snap = newest_ts
            lines.append(f"backup_coverage_entries{{{lbl}}} {entries.get(path, 0)}")
            lines.append(f"backup_coverage_volume_bytes{{{lbl}}} {sizes.get(volume, -1)}")
        else:
            due = since < now - DB_DUE_AFTER
            snap = db_newest.get(path, 0)
            is_present = snap > 0
        lines += [
            f"backup_coverage_expected{{{lbl}}} 1",
            f"backup_coverage_due{{{lbl}}} {int(due)}",
            f"backup_coverage_present{{{lbl}}} {int(is_present)}",
            f"backup_coverage_snapshot_timestamp{{{lbl}}} {snap}",
        ]
    lines += [
        "# HELP backup_coverage_last_success_timestamp When the last check completed.",
        "# TYPE backup_coverage_last_success_timestamp gauge",
        f"backup_coverage_last_success_timestamp {time.time()}",
    ]
    return "\n".join(lines) + "\n"


def loop():
    global metrics
    while True:
        try:
            metrics = collect()
            print(f"checked {metrics.count('backup_coverage_expected{')} paths", flush=True)
        except Exception as e:  # keep serving the last good result; staleness alerts
            print(f"check failed: {e}", flush=True)
        time.sleep(INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = metrics.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
