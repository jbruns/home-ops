"""Rollback Snapshots (GLOSSARY, ADR 0011): ZFS snapshots taken before a risky change, carrying the
user properties `homeops:issue` and `homeops:expires`, and released once the change is verified.

Usable without the CLI: `rollback_snapshots(client)` lists them for any `hops.truenas.Client`.
Only a snapshot whose own (LOCAL) properties include a `homeops:` one is a Rollback Snapshot: a
periodic-task or TrueNAS system snapshot never is, so it is never listed, reported or released.
"""
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from hops.errors import HopsError, UsageError
from hops.truenas import TrueNASError

ISSUE, EXPIRES = "homeops:issue", "homeops:expires"
# Docker's data-root on the Host (`docker info`: /mnt/.ix-apps/docker) is this dataset, which holds
# every named volume under volumes/. It has no child datasets, so its snapshot is not recursive.
DOCKER_DATASET = "luna/ix-apps/docker"
DEFAULT_DAYS = 7
PREFIX = "hops-"
# `used`: the space only this snapshot holds, which releasing it frees.
SIZE = "used"
QUERY_OPTIONS = {"extra": {"properties": [SIZE, ISSUE, EXPIRES]}}
DURATION = re.compile(r"(\d+)([dw])")


@dataclass(frozen=True)
class RollbackSnapshot:
    name: str  # <dataset>@<snapshot>
    dataset: str
    issue: str | None
    expires: str | None  # ISO date, UTC
    size: int | None  # bytes (the snapshot's `used`)
    expired: bool

    def to_json(self) -> dict:
        return asdict(self)


def now() -> datetime:
    return datetime.now(timezone.utc)


def today() -> date:
    return now().date()


def expired(expires: str | None, on: date) -> bool:
    """Past its release date: `homeops:expires` is earlier than `on` (UTC). A missing or unreadable
    date counts as expired, so the snapshot is reported rather than forgotten."""
    try:
        return date.fromisoformat(expires or "") < on
    except ValueError:
        return True


def own_property(snapshot: dict, key: str) -> str | None:
    """A user property set on the snapshot itself; inherited ones and unset ones are None."""
    prop = (snapshot.get("properties") or {}).get(key)
    if not isinstance(prop, dict) or prop.get("source") != "LOCAL":
        return None
    value = prop.get("value")
    return None if value in (None, "", "-") else str(value)


def is_rollback(snapshot: dict) -> bool:
    return own_property(snapshot, ISSUE) is not None or own_property(snapshot, EXPIRES) is not None


def record(snapshot: dict, on: date) -> RollbackSnapshot:
    size = (snapshot.get("properties") or {}).get(SIZE) or {}
    parsed = size.get("parsed")
    expires = own_property(snapshot, EXPIRES)
    return RollbackSnapshot(
        name=snapshot["id"], dataset=snapshot["dataset"], issue=own_property(snapshot, ISSUE),
        expires=expires, size=parsed if isinstance(parsed, int) else None, expired=expired(expires, on))


def rollback_snapshots(client, on: date | None = None) -> list[RollbackSnapshot]:
    """Every Rollback Snapshot on the Host, by name. `on` (default: today, UTC) decides `expired`.

    One `pool.snapshot.query` over every pool, asking only for the properties read here.
    """
    on = on or today()
    found = client.call("pool.snapshot.query", [], QUERY_OPTIONS)
    return sorted((record(s, on) for s in found if is_rollback(s)), key=lambda r: r.name)


def find(client, name: str) -> dict | None:
    found = client.call("pool.snapshot.query", [["id", "=", name]], QUERY_OPTIONS)
    return found[0] if found else None


def expiry(expires: str | None, duration: str | None, on: date) -> str:
    """The release date from --expires YYYY-MM-DD or --for <N>d/<N>w (default 7 days), not before `on`."""
    if expires:
        try:
            when = date.fromisoformat(expires)
        except ValueError:
            raise UsageError(f"--expires {expires!r} is not a date like 2026-10-30") from None
    elif duration:
        if not re.fullmatch(f"(?:{DURATION.pattern})+", duration):
            raise UsageError(f"--for {duration!r} is not a duration in days or weeks, like 7d or 2w")
        days = sum(int(n) * (7 if unit == "w" else 1) for n, unit in DURATION.findall(duration))
        when = on + timedelta(days=days)
    else:
        when = on + timedelta(days=DEFAULT_DAYS)
    if when < on:
        raise UsageError(f"release date {when.isoformat()} is already past")
    return when.isoformat()


def snapshot_name(issue: str, at: datetime) -> str:
    return f"{PREFIX}{issue}-{at.strftime('%Y%m%dT%H%M%SZ')}"


def check_issue(issue: str) -> None:
    if not re.fullmatch(r"\d+", issue):
        raise UsageError(f"--issue {issue!r} is not an issue number")


def created(client, names: list[str]) -> dict[str, datetime | None]:
    """Each snapshot's ZFS `creation`, by the Host's clock and to the whole second (None if unreadable)."""
    found = client.call("pool.snapshot.query", [["id", "in", names]], {"extra": {"properties": ["creation"]}})
    at = {}
    for s in found:
        raw = ((s.get("properties") or {}).get("creation") or {}).get("rawvalue")
        at[s["id"]] = datetime.fromtimestamp(int(raw), timezone.utc) if str(raw).isdigit() else None
    return at


def plan_take(client, issue: str, dataset: str, expires: str, at: datetime) -> dict:
    """What `take` would create, after checking the dataset exists and the name is free."""
    check_issue(issue)
    try:
        found = client.call("zfs.resource.query", {"paths": [dataset], "properties": []})
    except TrueNASError as exc:  # TrueNAS raises for a path that does not exist
        raise HopsError(f"cannot find dataset {dataset} on the Host: {exc}") from None
    if not found:
        raise HopsError(f"cannot find dataset {dataset} on the Host")
    name = snapshot_name(issue, at)
    if find(client, f"{dataset}@{name}"):
        raise HopsError(f"snapshot {dataset}@{name} already exists")
    return {"dataset": dataset, "name": name, "properties": {ISSUE: issue, EXPIRES: expires}}


def would_take(plan: dict) -> dict:
    """A plan as `--dry-run --json` prints it: name, dataset, issue and expires."""
    return {"name": f"{plan['dataset']}@{plan['name']}", "dataset": plan["dataset"],
            "issue": plan["properties"][ISSUE], "expires": plan["properties"][EXPIRES]}


def take(client, plan: dict) -> RollbackSnapshot:
    """Create the snapshot with its `homeops:` properties set at creation, and read it back."""
    client.call("pool.snapshot.create", plan)
    full = f"{plan['dataset']}@{plan['name']}"
    created = find(client, full)
    if not created or not is_rollback(created):
        raise HopsError(f"created {full}, but cannot read its {ISSUE} and {EXPIRES} back")
    return record(created, today())


def releasable(client, name: str) -> RollbackSnapshot:
    """The Rollback Snapshot `name` (<dataset>@<snapshot>), or an error: any other snapshot is refused."""
    if "@" not in name:
        matches = [r for r in rollback_snapshots(client) if r.name.split("@", 1)[1] == name]
        if len(matches) != 1:
            raise UsageError(f"{name}: give the full name <dataset>@<snapshot>"
                             + (f" ({', '.join(m.name for m in matches)})" if matches else
                                "; no Rollback Snapshot has that name (hops snapshot list)"))
        return matches[0]
    snapshot = find(client, name)
    if snapshot is None:
        raise HopsError(f"no snapshot {name} on the Host")
    if not is_rollback(snapshot):
        raise UsageError(f"refusing to release {name}: it has no {ISSUE} or {EXPIRES} of its own, so it is "
                         "not a Rollback Snapshot (periodic-task and system snapshots are never released)")
    return record(snapshot, today())


def release(client, snapshot: RollbackSnapshot) -> None:
    if client.call("pool.snapshot.delete", snapshot.name) is not True:
        raise HopsError(f"TrueNAS did not confirm releasing {snapshot.name}")
