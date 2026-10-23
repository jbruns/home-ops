"""Host-hygiene reads from TrueNAS, behind `hops host`: pools, dataset usage, and task status.

Task objects carry credentials, scripts, rclone arguments and job logs that can echo secrets, so
every record here is built from an allowlist of named fields, never by copying or filtering
TrueNAS's objects.
"""
import datetime

from hops import truenas

DATASET_PROPERTIES = ["mountpoint", "used", "available", "usedbysnapshots", "quota"]


def timestamp(value) -> str | None:
    """TrueNAS's `{"$date": <ms>}` as ISO 8601 UTC, e.g. 2026-10-23T06:00:00Z."""
    if not isinstance(value, dict) or not isinstance(value.get("$date"), (int, float)):
        return None
    when = datetime.datetime.fromtimestamp(value["$date"] / 1000, datetime.UTC)
    return when.isoformat(timespec="seconds").replace("+00:00", "Z")


def pools(client) -> list[dict]:
    out = []
    for p in client.call("pool.query", [], {"order_by": ["name"]}):
        size, allocated = p.get("size"), p.get("allocated")
        out.append({
            "name": p.get("name"), "status": p.get("status"), "healthy": p.get("healthy"),
            "size_bytes": size, "allocated_bytes": allocated, "free_bytes": p.get("free"),
            "used_percent": round(100 * allocated / size, 1) if size and allocated is not None else None,
        })
    return out


def dataset_at(client, path: str) -> str | None:
    """The ZFS dataset a path on the Host lives on, or None when it is not on ZFS."""
    fs = client.call("filesystem.statfs", path)
    return fs.get("source") if fs.get("fstype") == "zfs" else None


def dataset_usage(client, name: str) -> dict:
    """One dataset's space. Unlike pool.dataset.query, this also finds hidden ones (ix-apps)."""
    found = client.call("zfs.resource.query", {"paths": [name], "properties": DATASET_PROPERTIES})
    props = (found[0].get("properties") or {}) if found else {}

    def value(prop):
        v = (props.get(prop) or {}).get("value")
        return v if isinstance(v, int) else None

    return {
        "mountpoint": (props.get("mountpoint") or {}).get("value"),
        "used_bytes": value("used"), "available_bytes": value("available"),
        "snapshots_bytes": value("usedbysnapshots"), "quota_bytes": value("quota") or None,
    }


def _last_run(state: str | None, started: str | None, finished: str | None) -> dict | None:
    if state is None:
        return None
    return {"state": state, "started_at": started, "finished_at": finished}


def tasks(client) -> list[dict]:
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
        job = truenas.job_status(t["job"]) if isinstance(t.get("job"), dict) else {}
        out.append({
            "kind": "cloudsync", "id": t.get("id"), "name": t.get("description"), "source": t.get("path"),
            "enabled": t.get("enabled"),
            "last_run": _last_run(job.get("state"), timestamp(job.get("time_started")),
                                  timestamp(job.get("time_finished"))),
        })
    return out
