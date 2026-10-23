"""hops snapshot take|list|release, and expired Rollback Snapshots in hops strays, against FakeTrueNAS."""
import json
from datetime import date, datetime, timezone

import pytest
from hops_fakes import FakeTrueNAS, FakeTrueNASError
from test_hops_strays import answer, host  # noqa: F401 (fixture)

from hops import snapshots, truenas

NOW = datetime(2026, 10, 23, 15, 47, 5, tzinfo=timezone.utc)
DOCKER = "luna/ix-apps/docker"
PERIODIC = f"{DOCKER}@luna-2026-10-19_23-00"


def prop(value, source="LOCAL") -> dict:
    return {"value": str(value), "rawvalue": str(value), "source": source, "parsed": value}


def snap(name: str, used: int = 1024, **user) -> dict:
    dataset, short = name.split("@")
    return {"id": name, "name": name, "dataset": dataset, "snapshot_name": short, "pool": "luna",
            "type": "SNAPSHOT", "properties": {"used": prop(used, "NONE"), **user}}


class Pool:
    """TrueNAS's pool.snapshot.* methods over an in-memory set of snapshots."""

    def __init__(self, *found: dict):
        self.snapshots = {s["id"]: s for s in found}
        self.datasets = {DOCKER, "luna/apps"}
        self.server = FakeTrueNAS({
            "pool.snapshot.query": self.query, "pool.snapshot.create": self.create,
            "pool.snapshot.delete": self.delete, "zfs.resource.query": self.resources,
        })
        self.client = truenas.Client(truenas.TrueNASConfig("wss://tn.example/api/current", FakeTrueNAS.KEY),
                                     connect=self.server.connect)

    def query(self, filters, options):
        assert options == {"extra": {"properties": ["used", "homeops:issue", "homeops:expires"]}}
        return [s for s in self.snapshots.values() if all(s[k] == v for k, _, v in filters)]

    def create(self, data):
        name = f"{data['dataset']}@{data['name']}"
        self.snapshots[name] = snap(name, 0, **{k: prop(v) for k, v in data["properties"].items()})
        return self.snapshots[name]

    def delete(self, name):
        del self.snapshots[name]
        return True

    def resources(self, query):
        missing = [p for p in query["paths"] if p not in self.datasets]
        if missing:
            raise FakeTrueNASError("EINVAL", f"[ENOENT] zfs.resource.query: {missing[0]!r}: not found")
        return [{"name": p} for p in query["paths"]]

    def called(self, method):
        return [r["params"] for r in self.server.requests if r["method"] == method]


def rollback(name, issue="637", expires="2026-10-30", **kw):
    return snap(name, **{"homeops:issue": prop(issue), "homeops:expires": prop(expires)}, **kw)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(snapshots, "now", lambda: NOW)


def test_take_snapshots_dockers_data_root_with_its_properties_set_at_creation(hops):
    pool = Pool()
    outcome = hops("snapshot", "take", "--issue", "637", "--json", truenas=pool.client)
    assert outcome.code == 0, outcome.err
    assert pool.called("pool.snapshot.create") == [[{
        "dataset": DOCKER, "name": "hops-637-20261023T154705Z",
        "properties": {"homeops:issue": "637", "homeops:expires": "2026-10-30"}}]]
    assert json.loads(outcome.out)["snapshot"] == {
        "name": f"{DOCKER}@hops-637-20261023T154705Z", "dataset": DOCKER, "issue": "637",
        "expires": "2026-10-30", "size": 0, "expired": False}


@pytest.mark.parametrize("when, expires", [(["--for", "2w"], "2026-11-06"), (["--for", "1d"], "2026-10-24"),
                                           (["--expires", "2026-12-01"], "2026-12-01")])
def test_take_sets_the_release_date(hops, when, expires):
    pool = Pool()
    outcome = hops("snapshot", "take", "--issue", "9", "--dataset", "luna/apps", *when, truenas=pool.client)
    assert outcome.code == 0, outcome.err
    assert outcome.out.splitlines()[0] == "luna/apps@hops-9-20261023T154705Z"
    assert pool.snapshots["luna/apps@hops-9-20261023T154705Z"]["properties"]["homeops:expires"]["value"] == expires


@pytest.mark.parametrize("args, message", [
    (["--expires", "2026-10-22"], "already past"),
    (["--for", "soon"], "not a duration"),
    (["--issue", "abc"], "not an issue number"),
])
def test_take_refuses_bad_input_before_creating(hops, args, message):
    pool = Pool()
    outcome = hops("snapshot", "take", "--issue", "637", *args, truenas=pool.client)
    assert outcome.code == 2 and message in outcome.err
    assert pool.called("pool.snapshot.create") == []


def test_take_checks_the_dataset_exists(hops):
    pool = Pool()
    outcome = hops("snapshot", "take", "--issue", "637", "--dataset", "luna/nope", truenas=pool.client)
    assert outcome.code == 1 and "cannot find dataset luna/nope" in outcome.err
    assert pool.called("pool.snapshot.create") == []


def test_take_dry_run_creates_nothing(hops):
    pool = Pool()
    outcome = hops("snapshot", "take", "--issue", "637", "--dry-run", truenas=pool.client)
    assert outcome.code == 0, outcome.err
    assert f"Would take {DOCKER}@hops-637-20261023T154705Z" in outcome.out
    assert pool.snapshots == {}


def test_list_shows_only_snapshots_with_their_own_homeops_properties(hops):
    pool = Pool(
        rollback(f"{DOCKER}@hops-1-20261001T000000Z", issue="1", expires="2026-10-22", used=5 * 2**30),
        rollback(f"{DOCKER}@hops-2-20261020T000000Z", issue="2", expires="2026-10-23"),
        snap(PERIODIC),
        # Inherited from a dataset someone tagged: not a Rollback Snapshot.
        snap("luna/apps@auto-1", **{"homeops:issue": prop("3", "INHERITED")}),
    )
    outcome = hops("snapshot", "list", "--json", truenas=pool.client)
    assert outcome.code == 0, outcome.err
    # Expired means the release date is before today (UTC); on the day itself it has not.
    assert json.loads(outcome.out) == {"snapshots": [
        {"name": f"{DOCKER}@hops-1-20261001T000000Z", "dataset": DOCKER, "issue": "1", "expires": "2026-10-22",
         "size": 5 * 2**30, "expired": True},
        {"name": f"{DOCKER}@hops-2-20261020T000000Z", "dataset": DOCKER, "issue": "2", "expires": "2026-10-23",
         "size": 1024, "expired": False},
    ]}
    table = hops("snapshot", "list", truenas=pool.client).out
    assert "5.0GiB" in table and "luna-2026" not in table and "auto-1" not in table


def test_rollback_snapshots_is_usable_without_the_cli():
    pool = Pool(rollback(f"{DOCKER}@hops-1-x", expires="2026-10-01"), snap(PERIODIC))
    assert [(r.name, r.expired) for r in snapshots.rollback_snapshots(pool.client, date(2026, 9, 1))] == [
        (f"{DOCKER}@hops-1-x", False)]


def test_release_destroys_a_rollback_snapshot(hops):
    name = f"{DOCKER}@hops-637-20261023T154705Z"
    pool = Pool(rollback(name), snap(PERIODIC))
    outcome = hops("snapshot", "release", name, truenas=pool.client)
    assert outcome.code == 0, outcome.err
    assert outcome.out == f"Released {name} (issue #637).\n"
    assert list(pool.snapshots) == [PERIODIC]


def test_release_takes_a_unique_short_name(hops):
    pool = Pool(rollback(f"{DOCKER}@hops-637-x"))
    assert hops("snapshot", "release", "hops-637-x", truenas=pool.client).code == 0
    assert pool.snapshots == {}


@pytest.mark.parametrize("name", [PERIODIC, "luna-2026-10-19_23-00", "luna/apps@auto-1"])
def test_release_refuses_a_snapshot_without_its_own_homeops_properties(hops, name):
    pool = Pool(snap(PERIODIC), snap("luna/apps@auto-1", **{"homeops:issue": prop("3", "INHERITED")}))
    outcome = hops("snapshot", "release", name, truenas=pool.client)
    assert outcome.code == 2, outcome.out
    assert pool.called("pool.snapshot.delete") == []
    assert len(pool.snapshots) == 2


def test_release_dry_run_destroys_nothing(hops):
    name = f"{DOCKER}@hops-637-x"
    pool = Pool(rollback(name))
    outcome = hops("snapshot", "release", name, "--dry-run", truenas=pool.client)
    assert outcome.code == 0 and outcome.out.startswith(f"Would release {name}")
    assert list(pool.snapshots) == [name]


# hops strays

def test_strays_reports_expired_rollback_snapshots(hops, fake_host):
    answer(fake_host)
    pool = Pool(rollback(f"{DOCKER}@hops-1-x", issue="1", expires="2026-10-22"),
                rollback(f"{DOCKER}@hops-2-x", issue="2", expires="2026-10-23"),
                snap(PERIODIC))
    outcome = hops("strays", "--json", truenas=pool.client)
    assert outcome.code == 0, outcome.err
    report = json.loads(outcome.out)
    assert report["snapshots"] == [
        {"snapshot": f"{DOCKER}@hops-1-x", "dataset": DOCKER, "issue": "1", "expires": "2026-10-22", "size": 1024,
         "kept": "released only with hops snapshot release, once its change is verified", "removed": None}]
    assert report["snapshots_unchecked"] is None
    text = hops("strays", truenas=pool.client).out
    assert "Expired Rollback Snapshots:" in text and "hops-2-x" not in text


@pytest.mark.usefixtures("host")  # a fake Host and an origin/main to classify against
def test_strays_apply_never_releases_a_snapshot(hops):
    pool = Pool(rollback(f"{DOCKER}@hops-1-x", expires="2026-10-01"))
    outcome = hops("strays", "--apply", "--json", truenas=pool.client)
    assert outcome.code == 0, outcome.err
    assert json.loads(outcome.out)["snapshots"][0]["removed"] is None
    assert pool.called("pool.snapshot.delete") == []


def test_strays_without_truenas_says_snapshots_were_not_checked(hops, fake_host):
    answer(fake_host)
    outcome = hops("strays", "--json")
    assert outcome.code == 0, outcome.err
    report = json.loads(outcome.out)
    assert report["snapshots"] is None
    assert report["snapshots_unchecked"].startswith("TrueNAS is not available on this host")
    assert "Rollback Snapshots not checked: TrueNAS is not available" in hops("strays").out
