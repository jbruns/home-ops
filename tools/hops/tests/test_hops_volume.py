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
CREATED = 1792627200  # SNAPSHOT's ZFS creation, by the Host's clock: 2026-10-22T00:00:00Z
OLD, NEW = "home_mariadb_data", "nextcloud_mariadb_data"


@pytest.fixture(autouse=True)
def setup(repo, monkeypatch):
    monkeypatch.setattr(snapshots, "now", lambda: NOW)
    compose = repo / "tn" / "monitoring" / "compose.yaml"
    compose.write_text(compose.read_text() + f"  dependencies-render:\n    image: {PYTHON}\n"
                       "    container_name: dependencies-render\n")


def volume(name, docker_root=DOCKER_ROOT, **over) -> dict:
    return {"Name": name, "Driver": "local", "Labels": {}, "Options": None, "Scope": "local",
            "Mountpoint": f"{docker_root}/volumes/{name}/_data", **over}


def mounting(name, vol, **kw) -> dict:
    data = container(name, **kw)
    data["Mounts"] = [{"Type": "volume", "Name": vol, "Destination": "/data"}]
    return data


class Host:
    """The Docker side of a volume move: volumes as directories under `root`, and containers that mount them.

    `docker run` of the move container runs volumes.SCRIPT on this machine against `root`, reporting
    the filesystem as the ZFS dataset `dataset`.
    """

    def __init__(self, fake_host, root: Path, dataset=DATASET, fstype="zfs", docker_root=DOCKER_ROOT):
        self.fake, self.root, self.dataset, self.fstype, self.docker_root = fake_host, root, dataset, fstype, docker_root
        self.containers: dict[str, list[dict]] = {}
        self.runs: list[str] = []
        fake_host.respond("info", stdout=json.dumps(docker_root) + "\n")
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
        return json.dumps([volume(name, self.docker_root, **self.overrides.get(name, {}))])

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
        assert root == volumes.ROOT and args[args.index("-v") + 1].startswith(f"{self.docker_root}/volumes:{root}")
        self.runs.append(mode)
        done = subprocess.run([sys.executable, "-c", args[-5], mode, str(self.root), old, new],
                              capture_output=True, text=True)
        report = json.loads(done.stdout)
        report["mount"] = [self.fstype, self.dataset]
        return json.dumps(report)


@pytest.fixture
def host(fake_host, tmp_path):
    return Host(fake_host, tmp_path / "volumes")


@pytest.fixture
def pool():
    return Pool(rollback(SNAPSHOT, issue="638", expires="2026-10-30", creation=CREATED))


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
    assert host.runs == ["check", "move", "check"]  # <old> is probed again, empty, just before its removal
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


@pytest.mark.parametrize("old, new, issue", [("a;rm -rf /", NEW, "638"), (OLD, "x", "638"), (OLD, "../etc", "638"),
                                            (OLD, OLD, "638"), (OLD, NEW, "#638")])
def test_bad_input_is_refused_before_the_host_is_touched(hops, fake_host, pool, old, new, issue):
    outcome = hops("volume", "move", old, new, "--issue", issue, truenas=pool.client)
    assert outcome.code == 2 and fake_host.calls == []


def test_dockers_volumes_directory_comes_from_docker_info(hops, fake_host, tmp_path, pool):
    host = Host(fake_host, tmp_path / "volumes", docker_root="/var/lib/docker")
    host.add(OLD, {"f": "x"})
    assert move(hops, pool).code == 0
    assert (host.root / NEW / "_data" / "f").exists()


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


@pytest.mark.parametrize("name, project", [("someones-debug", None), ("ix-plex-plex-1", "ix-plex")])
def test_refuses_a_stopped_container_on_old_that_no_stack_declares(hops, host, pool, fake_host, name, project):
    host.add(OLD, {"f": "x"})
    host.containers[OLD] = [mounting(name, OLD, project=project, service="x", status="exited")]
    assert refused(move(hops, pool), f"{name} mounts")
    assert nothing_changed(host, fake_host)


def stopped_mariadb(host, finished_at):
    db = mounting("mariadb", OLD, project="home", service="mariadb", status="exited")
    db["State"]["FinishedAt"] = finished_at
    host.containers[OLD] = [db]
    return db


def test_a_snapshot_created_after_the_last_stop_covers_the_move(hops, host, pool):
    host.add(OLD, {"f": "x"})
    stopped_mariadb(host, "2026-10-21T23:59:59.999999999Z")
    assert move(hops, pool).code == 0


def test_refuses_a_snapshot_created_before_the_containers_on_old_stopped(hops, host, pool, fake_host):
    host.add(OLD, {"f": "x"})
    stopped_mariadb(host, "2026-10-22T00:00:01.5Z")
    assert refused(move(hops, pool), f"{SNAPSHOT} was created at 2026-10-22 00:00:00Z, before mariadb stopped "
                                     "(2026-10-22 00:00:01.500000Z)")
    assert nothing_changed(host, fake_host)


@pytest.mark.parametrize("finished_at", ["2026-10-22T00:00:00.000000001Z", "2026-10-22T00:00:00.5Z",
                                         "2026-10-22T00:00:00Z"])
def test_refuses_a_snapshot_created_in_the_second_the_containers_on_old_stopped(hops, host, pool, fake_host,
                                                                                finished_at):
    # ZFS keeps creation to the second, so a snapshot of that second may be from before the stop.
    host.add(OLD, {"f": "x"})
    stopped_mariadb(host, finished_at)
    assert refused(move(hops, pool), f"{SNAPSHOT} was created in the second mariadb stopped")
    assert nothing_changed(host, fake_host)


def test_freshness_is_the_hosts_creation_time_not_the_snapshots_name(hops, host, fake_host):
    # The name comes from the clock of the machine that ran `snapshot take`; creation from the Host's.
    host.add(OLD, {"f": "x"})
    stopped_mariadb(host, "2026-10-22T00:00:30Z")
    ahead = f"{DATASET}@hops-638-20261022T000100Z"  # named after the stop, created before it
    assert refused(move(hops, Pool(rollback(ahead, issue="638", creation=CREATED))), "before mariadb stopped")
    behind = f"{DATASET}@hops-638-20261021T235900Z"  # named before the stop, created after it
    outcome = move(hops, Pool(rollback(behind, issue="638", creation=CREATED + 60)), "--json")
    assert outcome.code == 0, outcome.err
    assert json.loads(outcome.out)["snapshot"]["name"] == behind


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


def test_refuses_old_without_a_data_directory(hops, host, pool, fake_host):
    host.add(OLD).rmdir()
    assert refused(move(hops, pool), f"{DOCKER_ROOT}/volumes/{OLD}/_data is not a directory")
    assert nothing_changed(host, fake_host)


def test_refuses_volumes_not_on_a_zfs_dataset(hops, fake_host, tmp_path, pool):
    host = Host(fake_host, tmp_path / "volumes", dataset="/dev/sda1", fstype="ext4")
    host.add(OLD, {"f": "x"})
    assert refused(move(hops, pool), "is on ext4, not a ZFS dataset")
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


def test_a_failed_removal_after_the_move_reports_what_was_removed(hops, host, pool, fake_host):
    host.add(OLD, {"f": "x"})
    host.containers[OLD] = [mounting("mariadb", OLD, project="home", service="mariadb", status="exited")]
    fake_host.respond("volume", "rm", stderr="volume is in use", returncode=1)
    outcome = move(hops, pool)
    assert outcome.code == 1
    assert f"removing {OLD} failed" in outcome.err and "containers removed: mariadb; still there: none" in outcome.err
    assert f"{OLD} holds 0 top-level entries, {NEW} holds 1 top-level entries" in outcome.err


def test_refuses_when_a_consumer_starts_between_the_checks_and_the_move(hops, host, pool, fake_host):
    # e.g. a Deploy of the Stack lands while the snapshot is taken and <new> created
    host.add(OLD, {"f": "x"})
    db = stopped_mariadb(host, "2026-10-21T00:00:00Z")
    create = host.create

    def deploy_starts_it(call):
        db["State"]["Status"] = "running"
        return create(call)
    fake_host.respond("volume", "create", stdout=deploy_starts_it)

    outcome = move(hops, pool)
    assert outcome.code == 1
    assert "mariadb (running) started" in outcome.err and "nothing was moved" in outcome.err
    assert f"{OLD} holds 1 top-level entries, {NEW} holds 0 top-level entries" in outcome.err
    assert f"Rollback Snapshot {SNAPSHOT}" in outcome.err
    assert host.runs == ["check", "check"]
    assert not any(c[0] == "rm" or c[:2] == ["volume", "rm"] for c in fake_host.docker_calls)


@pytest.mark.parametrize("on, status", [(OLD, "running"), (NEW, "running"), (OLD, "created")])
def test_a_consumer_appearing_during_the_move_stops_before_cleanup(hops, host, pool, fake_host, on, status):
    host.add(OLD, {"f": "x"})
    run = host.run

    def deploy_during_move(call):
        report = run(call)
        if call.docker[-1][-4] == "move":
            host.containers.setdefault(on, []).append(
                mounting("mariadb", on, project="nextcloud", service="mariadb", status=status))
        return report
    fake_host.respond("run", stdout=deploy_during_move)

    outcome = move(hops, pool)
    assert outcome.code == 1
    assert f"mariadb ({status}) started" in outcome.err and "during the move" in outcome.err
    assert f"{OLD} holds 0 top-level entries, {NEW} holds 1 top-level entries" in outcome.err
    assert f"Rollback Snapshot {SNAPSHOT}" in outcome.err
    assert not any(c[0] == "rm" or c[:2] == ["volume", "rm"] for c in fake_host.docker_calls)


def ran_and_exited(db, host, write=True):
    """A known consumer starts, writes to <old> and exits again: stopped as before, with new times."""
    db["State"].update(StartedAt="2026-10-23T15:47:06.1Z", FinishedAt="2026-10-23T15:47:07.2Z")
    if write:
        (host.root / OLD / "_data" / "written-meanwhile").write_text("new")


def test_a_known_consumer_that_ran_before_the_move_stops_it(hops, host, pool, fake_host):
    host.add(OLD, {"f": "x"})
    db = stopped_mariadb(host, "2026-10-21T00:00:00Z")
    create = host.create

    def runs_meanwhile(call):
        ran_and_exited(db, host, write=False)
        return create(call)
    fake_host.respond("volume", "create", stdout=runs_meanwhile)

    outcome = move(hops, pool)
    assert outcome.code == 1
    assert "mariadb (exited, ran since the checks) started" in outcome.err and "nothing was moved" in outcome.err
    assert "move" not in host.runs
    assert not any(c[0] == "rm" or c[:2] == ["volume", "rm"] for c in fake_host.docker_calls)


def test_a_known_consumer_that_ran_during_the_move_stops_before_cleanup(hops, host, pool, fake_host):
    host.add(OLD, {"f": "x"})
    db = stopped_mariadb(host, "2026-10-21T00:00:00Z")
    run = host.run

    def runs_during_move(call):
        report = run(call)
        if call.docker[-1][-4] == "move":
            ran_and_exited(db, host)
        return report
    fake_host.respond("run", stdout=runs_during_move)

    outcome = move(hops, pool)
    assert outcome.code == 1
    assert "mariadb (exited, ran since the checks) started during the move" in outcome.err
    assert f"Rollback Snapshot {SNAPSHOT}" in outcome.err
    assert not any(c[0] == "rm" or c[:2] == ["volume", "rm"] for c in fake_host.docker_calls)
    assert (host.root / OLD / "_data" / "written-meanwhile").read_text() == "new"


def test_old_is_kept_when_it_is_not_empty_just_before_its_removal(hops, host, pool, fake_host):
    host.add(OLD, {"f": "x"})
    host.containers[OLD] = [mounting("mariadb", OLD, project="home", service="mariadb", status="exited")]

    def something_writes(call):
        (host.root / OLD / "_data" / "late").write_text("new")
        return ""
    fake_host.respond("rm", stdout=something_writes)

    outcome = move(hops, pool)
    assert outcome.code == 1
    assert f"{OLD} is not empty" in outcome.err and "kept it" in outcome.err
    assert f"{OLD} holds 1 top-level entries, {NEW} holds 1 top-level entries" in outcome.err
    assert "holds 0" not in outcome.err and outcome.err.count("failed") == 1
    assert not any(c[:2] == ["volume", "rm"] for c in fake_host.docker_calls)
    assert (host.root / OLD / "_data" / "late").exists()
