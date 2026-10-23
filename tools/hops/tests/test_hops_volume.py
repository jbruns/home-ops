"""hops volume move: preconditions, the move itself (its script run here against a directory), and failure reports."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hops_fakes import FakeSnapshots as Pool
from hops_fakes import container, rollback

from hops import snapshots, volumes

NOW = datetime(2026, 10, 23, 15, 47, 5, tzinfo=timezone.utc)
PYTHON = "python:3.14.8-alpine3.24@sha256:4444"
DOCKER_ROOT = "/mnt/.ix-apps/docker"
DATASET = "luna/ix-apps/docker"
SNAPSHOT = f"{DATASET}@hops-638-20261022T000000Z"
OLD, NEW = "home_mariadb_data", "nextcloud_mariadb_data"


@pytest.fixture(autouse=True)
def setup(repo, monkeypatch):
    monkeypatch.setattr(snapshots, "now", lambda: NOW)
    compose = repo / "tn" / "monitoring" / "compose.yaml"
    compose.write_text(compose.read_text() + f"  dependencies-render:\n    image: {PYTHON}\n"
                       "    container_name: dependencies-render\n")


def volume(name, **over) -> dict:
    return {"Name": name, "Driver": "local", "Labels": {}, "Options": None, "Scope": "local",
            "Mountpoint": f"{DOCKER_ROOT}/volumes/{name}/_data", **over}


def mounting(name, vol, **kw) -> dict:
    data = container(name, **kw)
    data["Mounts"] = [{"Type": "volume", "Name": vol, "Destination": "/data"}]
    return data


class Host:
    """The Docker side of a volume move: volumes as directories under `root`, and containers that mount them.

    `docker run` of the move container runs volumes.SCRIPT on this machine against `root`, reporting
    the filesystem as the ZFS dataset `dataset`.
    """

    def __init__(self, fake_host, root: Path, dataset=DATASET):
        self.fake, self.root, self.dataset = fake_host, root, dataset
        self.containers: dict[str, list[dict]] = {}
        self.runs: list[str] = []
        fake_host.respond("info", stdout=json.dumps(DOCKER_ROOT) + "\n")
        fake_host.respond("volume", "inspect", stdout=self.inspect)
        fake_host.respond("volume", "create", stdout=self.create)
        fake_host.respond("volume", "rm", stdout=self.remove)
        fake_host.respond("rm", stdout=lambda call: "")
        fake_host.respond("ps", stdout=self.ps)
        fake_host.respond("inspect", stdout=self.inspect_containers)
        fake_host.respond("run", stdout=self.run)
        self.overrides: dict[str, dict] = {}

    def add(self, name, files: dict[str, str] | None = None, **over):
        data = self.root / name / "_data"
        data.mkdir(parents=True)
        for rel, text in (files or {}).items():
            (data / rel).parent.mkdir(parents=True, exist_ok=True)
            (data / rel).write_text(text)
        self.overrides[name] = over
        return data

    def inspect(self, call):
        name = call.docker[-1][-1]
        if not (self.root / name).is_dir():
            # FakeHost answers with stdout only; a missing volume is an empty list, as Docker's JSON would be.
            return "[]"
        return json.dumps([volume(name, **self.overrides.get(name, {}))])

    def create(self, call):
        (self.root / call.docker[-1][-1] / "_data").mkdir(parents=True)
        return call.docker[-1][-1] + "\n"

    def remove(self, call):
        os.rmdir(self.root / call.docker[-1][-1] / "_data")
        os.rmdir(self.root / call.docker[-1][-1])
        return ""

    def ps(self, call):
        name = call.docker[-1][-1].removeprefix("volume=")
        return "".join(c["Id"] + "\n" for c in self.containers.get(name, []))

    def inspect_containers(self, call):
        ids = set(call.docker[-1][1:])
        return json.dumps([c for cs in self.containers.values() for c in cs if c["Id"] in ids])

    def run(self, call):
        args = call.docker[-1]
        mode, root, old, new = args[-4:]
        assert root == volumes.ROOT and f"{DOCKER_ROOT}/volumes:{root}" in args[args.index("-v") + 1]
        self.runs.append(mode)
        done = subprocess.run([sys.executable, "-c", args[-5], mode, str(self.root), old, new],
                              capture_output=True, text=True)
        report = json.loads(done.stdout)
        report["mount"] = ["zfs", self.dataset]
        return json.dumps(report)


@pytest.fixture
def host(fake_host, tmp_path):
    return Host(fake_host, tmp_path / "volumes")


@pytest.fixture
def pool():
    return Pool(rollback(SNAPSHOT, issue="638", expires="2026-10-30"))


def move(hops, pool, *extra):
    return hops("volume", "move", OLD, NEW, "--issue", "638", *extra, truenas=pool.client if pool else None)


def test_move_renames_every_entry_and_removes_the_old_volume(hops, host, pool, fake_host):
    data = host.add(OLD, {".hidden": "dot", "nested/deep/file": "x", "with space": "s", "line\nbreak": "n"})
    (data / "nested").chmod(0o750)
    data.chmod(0o710)
    outside = host.root.parent / "outside"
    outside.mkdir()
    (outside / "target").write_text("keep")
    (data / "link").symlink_to(outside)
    host.containers[OLD] = [mounting("mariadb", OLD, project="home", service="mariadb", status="exited")]

    outcome = move(hops, pool, "--json")

    assert outcome.code == 0, outcome.err
    new = host.root / NEW / "_data"
    assert sorted(p.name for p in new.iterdir()) == [".hidden", "line\nbreak", "link", "nested", "with space"]
    assert (new / "nested" / "deep" / "file").read_text() == "x"
    assert (new / "nested").stat().st_mode & 0o777 == 0o750
    assert new.stat().st_mode & 0o777 == 0o710
    assert (new / "link").is_symlink() and (outside / "target").read_text() == "keep"
    assert not (host.root / OLD).exists()
    report = json.loads(outcome.out)
    assert report["moved"] is True and report["entries"] == 5 and report["snapshot"]["name"] == SNAPSHOT
    assert report["containers"][0]["service"] == "nextcloud/mariadb"
    assert host.runs == ["check", "move"]
    # The stopped container is removed (by ID) before the volume, which Docker refuses while one references it.
    removals = [c[:2] for c in fake_host.docker_calls if c[0] == "rm" or c[:2] == ["volume", "rm"]]
    assert removals == [["rm", report["containers"][0]["id"]], ["volume", "rm"]]


def test_move_prints_the_next_steps(hops, host, pool):
    host.add(OLD, {"f": "x"})
    outcome = move(hops, pool)
    assert outcome.code == 0, outcome.err
    assert "Moved 1 top-level entries" in outcome.out
    assert f"hops snapshot release {SNAPSHOT}" in outcome.out


def test_take_snapshot_snapshots_the_dataset_holding_the_volumes(hops, host, fake_host):
    host.add(OLD, {"f": "x"})
    pool = Pool()
    outcome = move(hops, pool, "--take-snapshot", "--json")
    assert outcome.code == 0, outcome.err
    assert pool.called("pool.snapshot.create")[0][0]["dataset"] == DATASET
    assert json.loads(outcome.out)["snapshot"]["issue"] == "638"


def test_dry_run_checks_everything_and_changes_nothing(hops, host, fake_host):
    host.add(OLD, {"f": "x"})
    pool = Pool()
    outcome = move(hops, pool, "--take-snapshot", "--dry-run")
    assert outcome.code == 0, outcome.err
    assert "Nothing changed" in outcome.out and "would take it" in outcome.out
    assert pool.snapshots == {} and host.runs == ["check"]
    assert not (host.root / NEW).exists() and (host.root / OLD / "_data" / "f").exists()


def test_an_existing_empty_new_volume_is_used(hops, host, pool, fake_host):
    host.add(OLD, {"f": "x"})
    host.add(NEW)
    assert move(hops, pool).code == 0
    assert not any(c[:2] == ["volume", "create"] for c in fake_host.docker_calls)
    assert (host.root / NEW / "_data" / "f").exists()


def test_a_name_off_the_adr_0010_convention_only_warns(hops, host, pool):
    host.add(OLD, {"f": "x"})
    outcome = hops("volume", "move", OLD, "scratch-new", "--issue", "638", truenas=pool.client)
    assert outcome.code == 0, outcome.err
    assert "warning: scratch-new does not follow ADR 0010" in outcome.err


@pytest.mark.parametrize("old, new", [("a;rm -rf /", NEW), (OLD, "x"), (OLD, "../etc"), (OLD, OLD)])
def test_bad_names_are_refused_before_the_host_is_touched(hops, fake_host, pool, old, new):
    outcome = hops("volume", "move", old, new, "--issue", "638", truenas=pool.client)
    assert outcome.code == 2 and fake_host.calls == []


def refused(outcome, text):
    assert outcome.code == 2, outcome.out
    assert "refusing to move" in outcome.err and text in outcome.err, outcome.err
    return True


def nothing_changed(host, fake_host):
    return not any(c[0] in ("rm",) or c[:2] in (["volume", "create"], ["volume", "rm"])
                   for c in fake_host.docker_calls) and "move" not in host.runs


def test_refuses_a_missing_old_volume(hops, host, pool, fake_host):
    assert refused(move(hops, pool), f"there is no volume {OLD}")
    assert nothing_changed(host, fake_host)


@pytest.mark.parametrize("over, text", [
    ({"Options": {"type": "none", "o": "bind", "device": "/mnt/tank/x"}}, "driver options (device=/mnt/tank/x"),
    ({"Driver": "nfs"}, "driver is nfs"),
    ({"Mountpoint": "/elsewhere/_data"}, "Mountpoint /elsewhere/_data is not"),
])
def test_refuses_a_volume_whose_data_is_not_under_dockers_volumes_directory(hops, host, pool, fake_host, over, text):
    host.add(OLD, {"f": "x"}, **over)
    assert refused(move(hops, pool), text)
    assert nothing_changed(host, fake_host)


@pytest.mark.parametrize("status", ["running", "restarting", "paused"])
def test_refuses_while_a_container_mounting_old_is_not_stopped(hops, host, pool, fake_host, status):
    host.add(OLD, {"f": "x"})
    host.containers[OLD] = [mounting("mariadb", OLD, project="home", service="mariadb", status=status)]
    assert refused(move(hops, pool), f"mariadb ({status}) still mounts {OLD}")
    assert nothing_changed(host, fake_host)


def test_refuses_a_stopped_container_on_old_that_no_stack_declares(hops, host, pool, fake_host):
    host.add(OLD, {"f": "x"})
    host.containers[OLD] = [mounting("someones-debug", OLD, status="exited")]
    assert refused(move(hops, pool), "someones-debug mounts")
    assert nothing_changed(host, fake_host)


def test_a_stopped_deliberate_container_on_old_is_removed(hops, host, pool, fake_host):
    host.add(OLD, {"f": "x"})
    helper = mounting("helper", OLD, status="exited")
    helper["Config"]["Labels"]["startup-order"] = "skip"
    host.containers[OLD] = [helper]
    assert move(hops, pool).code == 0
    assert ["rm", helper["Id"]] in fake_host.docker_calls


def test_refuses_while_a_container_mounting_new_is_running(hops, host, pool, fake_host):
    host.add(OLD, {"f": "x"})
    host.add(NEW)
    host.containers[NEW] = [mounting("mariadb", NEW, project="nextcloud", service="mariadb")]
    assert refused(move(hops, pool), f"mariadb (running) mounts {NEW}")


def test_refuses_a_new_volume_that_is_not_empty(hops, host, pool, fake_host):
    host.add(OLD, {"f": "x"})
    host.add(NEW, {".already": "here"})
    assert refused(move(hops, pool), f"{NEW} exists and is not empty (1 top-level entries)")
    assert nothing_changed(host, fake_host)


@pytest.mark.parametrize("found", [
    [],
    [rollback(SNAPSHOT, issue="638", expires="2026-10-22")],  # expired
    [rollback(SNAPSHOT, issue="999")],  # another change's
    [rollback("luna/apps@hops-638-x", issue="638")],  # another dataset's
])
def test_refuses_without_an_unexpired_rollback_snapshot_of_the_volumes_dataset(hops, host, fake_host, found):
    host.add(OLD, {"f": "x"})
    assert refused(move(hops, Pool(*found)), f"no unexpired Rollback Snapshot of {DATASET}")
    assert nothing_changed(host, fake_host)


def test_refuses_without_truenas(hops, host, fake_host):
    host.add(OLD, {"f": "x"})
    assert refused(move(hops, None), "needs a Rollback Snapshot")
    assert nothing_changed(host, fake_host)


def test_refuses_volumes_on_different_filesystems(hops, host, pool, fake_host, monkeypatch):
    host.add(OLD, {"f": "x"})
    run = host.run

    def other_device(call):
        report = json.loads(run(call))
        report["old"]["dev"] += 1
        return json.dumps(report)
    fake_host.respond("run", stdout=other_device)
    assert refused(move(hops, pool), "not all on the filesystem")
    assert nothing_changed(host, fake_host)


def test_a_failure_part_way_reports_both_volumes_and_rolls_nothing_back(hops, host, pool, fake_host):
    host.add(OLD, {"a": "1", "b": "2", "c": "3"})
    run = host.run

    def fails_after_one(call):
        if call.docker[-1][-4] == "check":
            return run(call)
        report = json.loads(run(call))  # moves everything, then report as if it stopped after one
        report.update(moved=1, error="PermissionError: [Errno 13] Permission denied",
                      old={**report["old"], "entries": 2}, new={**report["new"], "entries": 1})
        return json.dumps(report)
    fake_host.respond("run", stdout=fails_after_one)

    outcome = move(hops, pool)
    assert outcome.code == 1
    assert "the move failed: PermissionError" in outcome.err and "after 1 of 3 entries" in outcome.err
    assert f"{OLD} holds 2 top-level entries, {NEW} holds 1 top-level entries" in outcome.err
    assert f"Rollback Snapshot {SNAPSHOT}" in outcome.err and "rolled nothing back" in outcome.err
    assert not any(c[0] == "rm" or c[:2] == ["volume", "rm"] for c in fake_host.docker_calls)
