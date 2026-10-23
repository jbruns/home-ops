"""hops host: pools, datasets, Docker usage and task status, from recorded TrueNAS and docker output."""
import json
from pathlib import Path

import pytest
from datetime import datetime, timezone

from hops_fakes import FakeTrueNAS, FakeTrueNASError, container, rollback, snap, write

from hops import cli, snapshots, truenas
from hops.errors import HopsError

FIXTURES = Path(__file__).parent / "fixtures" / "host"
RECORDED = json.loads((FIXTURES / "truenas.json").read_text())
SYSTEM_DF = (FIXTURES / "system_df.jsonl").read_text()
CFG = truenas.TrueNASConfig(url="wss://tn.example/api/current", api_key=FakeTrueNAS.KEY)
SECRET = "fake-leaked-secret"
SNAPSHOTS = [
    snap("luna/ix-apps/docker@luna-2026-10-22_23-00", used=4096),  # periodic: not a Rollback Snapshot
    rollback("luna/ix-apps/docker@hops-637-20261015T120000Z", issue="637", expires="2026-10-22", used=3 * 2**30),
    rollback("andromeda/media@hops-669-20261023T150000Z", issue="669", expires="2026-10-30", used=2**30),
]

STACK_BACK = {
    "tn/ops/compose.yaml": """
        services:
          stack-back:
            image: ghcr.io/lawndoc/stack-back:v1.5.4@sha256:4444
            container_name: stack-back
            labels: {tier: critical}
            volumes:
              - /var/run/docker.sock:/tmp/docker.sock:ro
              - ${ZFS_POOL_ROOT_HDD}/docker-backups:/backups
    """,
}
STACK_BACK_MOUNTS = [
    {"Type": "bind", "Source": "/mnt/andromeda/docker-backups", "Destination": "/backups"},
    {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/tmp/docker.sock"},
    {"Type": "volume", "Name": "common_backup_cache",
     "Source": "/mnt/.ix-apps/docker/volumes/common_backup_cache/_data", "Destination": "/cache"},
]


def resource_query(options):
    by_name = {d["name"]: d for d in RECORDED["zfs.resource.query"]}
    return [by_name[p] for p in options["paths"]]


def recorded_methods() -> dict:
    return {
        "pool.query": lambda *p: RECORDED["pool.query"],
        "pool.snapshottask.query": lambda *p: RECORDED["pool.snapshottask.query"],
        "cloudsync.query": lambda *p: RECORDED["cloudsync.query"],
        "filesystem.statfs": lambda path: RECORDED["filesystem.statfs"][path],
        "zfs.resource.query": resource_query,
        "pool.snapshot.query": lambda *p: SNAPSHOTS,
    }


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(snapshots, "now", lambda: datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc))


@pytest.fixture
def server() -> FakeTrueNAS:
    return FakeTrueNAS(recorded_methods())


@pytest.fixture
def host(fake_host, repo):
    write(repo, STACK_BACK)
    fake_host.respond("system", "df", stdout=SYSTEM_DF)
    fake_host.respond("info", stdout='"/mnt/.ix-apps/docker"\n')
    stack_back = container("stack-back", project="ops", service="stack-back")
    stack_back["Mounts"] = STACK_BACK_MOUNTS
    fake_host.respond("inspect", "stack-back", stdout=json.dumps([stack_back]))
    return fake_host


@pytest.fixture
def run_host(repo, runner, host, server, capsys):
    """hops host with the recorded TrueNAS; `env` holds settings, `truenas=False` leaves it unconfigured."""
    def run(*argv, env=None, truenas_on=True):
        client = truenas.Client(CFG, connect=server.connect) if truenas_on else None
        ctx = cli.Context(repo_root=repo, runner=runner, truenas=client, env=env or {})
        code = cli.main(["host", *argv], ctx=ctx)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def test_json_reports_pools_datasets_docker_and_tasks(run_host):
    code, out, err = run_host("--json", env={"HOPS_HOST_DATASETS": "luna"})
    assert (code, err) == (0, "")
    report = json.loads(out)

    assert report["pools"][1] == {
        "name": "luna", "status": "ONLINE", "healthy": True, "size_bytes": 3831110828032,
        "allocated_bytes": 588037693440, "free_bytes": 3243073134592, "used_percent": 15.3}
    assert [(d["name"], d["roles"]) for d in report["datasets"]] == [
        ("luna/ix-apps/docker", ["docker"]), ("andromeda/docker-backups", ["restic"]), ("luna", ["configured"])]
    assert report["datasets"][1] == {
        "name": "andromeda/docker-backups", "roles": ["restic"], "mountpoint": "/mnt/andromeda/docker-backups",
        "used_bytes": 1836465068256, "available_bytes": 149728127125120, "snapshots_bytes": 0, "quota_bytes": None}
    assert report["docker"][0] == {
        "type": "images", "total": 107, "active": 50, "size": "61.48GB", "size_bytes": 61480000000,
        "reclaimable": "36.53GB (59%)", "reclaimable_bytes": 36530000000, "reclaimable_percent": 59}
    assert [d["type"] for d in report["docker"]] == ["images", "containers", "volumes", "build_cache"]
    assert report["docker"][3]["reclaimable_percent"] is None
    assert report["rollback_snapshots"] == {
        "count": 2, "size_bytes": 4 * 2**30,
        "expired": [{"name": "luna/ix-apps/docker@hops-637-20261015T120000Z", "dataset": "luna/ix-apps/docker",
                     "issue": "637", "expires": "2026-10-22", "size": 3 * 2**30, "expired": True}]}
    assert report["tasks"] == [
        {"kind": "snapshot", "id": 1, "name": "luna-%Y-%m-%d_%H-%M", "source": "luna", "enabled": True,
         "last_run": {"state": "FINISHED", "started_at": None, "finished_at": "2026-10-23T06:00:00Z"}},
        {"kind": "cloudsync", "id": 3, "name": "Nightly restic repository", "source": "/mnt/andromeda/docker-backups",
         "enabled": True,
         "last_run": {"state": "SUCCESS", "started_at": "2026-10-23T11:00:01Z", "finished_at": "2026-10-23T11:00:23Z"}},
    ]
    assert report["notes"] == []


def test_plain_output_has_a_section_each(run_host):
    code, out, _ = run_host()
    assert code == 0
    for heading in ("Pools", "Datasets", "Docker", "Rollback Snapshots", "Tasks"):
        assert f"\n{heading}\n" in f"\n{out}"
    assert "andromeda  ONLINE" in out and "436.5 TiB" in out  # pool size, binary units
    assert "images" in out and "36.53GB (59%)" in out
    assert "cloudsync  3" in out and "SUCCESS" in out
    assert "2 Rollback Snapshots, 4.0 GiB; 1 expired:" in out
    assert "luna/ix-apps/docker@hops-637-20261015T120000Z  637    2026-10-22  3.0 GiB" in out


def test_task_status_never_shows_credentials_scripts_or_job_output(run_host, server):
    def seeded(task, allowed):
        return {k: v if k in allowed else SECRET if not isinstance(v, dict) else {"type": "B2", "key": SECRET}
                for k, v in task.items()}

    snapshot_task = seeded(RECORDED["pool.snapshottask.query"][0], {"id", "naming_schema", "dataset", "enabled"})
    snapshot_task["state"] = {"state": "ERROR", "datetime": {"$date": 1792735200000}, "error": SECRET,
                              "last_snapshot": SECRET}
    cloud = seeded(RECORDED["cloudsync.query"][0], {"id", "description", "path", "enabled", "job"})
    cloud["job"] = dict(cloud["job"], state="FAILED", error=SECRET, logs_excerpt=SECRET, exception=SECRET,
                        result=SECRET, arguments=[SECRET], progress={"percent": 50, "description": SECRET})
    server.methods["pool.snapshottask.query"] = lambda *p: [snapshot_task]
    server.methods["cloudsync.query"] = lambda *p: [cloud]

    for argv in ((), ("--json",)):
        code, out, err = run_host(*argv)
        assert code == 0
        assert SECRET not in out + err
    tasks = json.loads(out)["tasks"]
    assert [t["last_run"]["state"] for t in tasks] == ["ERROR", "FAILED"]


def test_configured_dataset_that_does_not_exist_is_a_note(run_host, server):
    def query(options):
        if options["paths"] == ["tank/gone"]:
            raise FakeTrueNASError("EINVAL", "[ENOENT] zfs.resource.query: 'tank/gone': not found")
        return resource_query(options)

    server.methods["zfs.resource.query"] = query
    code, out, _ = run_host("--json", env={"HOPS_HOST_DATASETS": "tank/gone, luna/ix-apps/docker"})
    report = json.loads(out)
    assert code == 0
    assert [(d["name"], d["roles"]) for d in report["datasets"]] == [
        ("luna/ix-apps/docker", ["docker", "configured"]), ("andromeda/docker-backups", ["restic"])]
    assert any("tank/gone" in n for n in report["notes"])


def test_without_truenas_only_docker_is_shown(run_host, host, server):
    code, out, err = run_host("--json", truenas_on=False)
    assert (code, err) == (0, "")
    report = json.loads(out)
    assert (report["pools"], report["datasets"], report["rollback_snapshots"], report["tasks"]) == (None,) * 4
    assert len(report["docker"]) == 4
    assert report["notes"] and "TrueNAS is not available on this host" in report["notes"][0]
    assert server.requests == []
    assert host.docker_calls == [["system", "df", "--format", "json"]]

    code, out, _ = run_host(truenas_on=False)
    assert "Docker" in out and "Pools" not in out and "TrueNAS is not available" in out


def test_unreachable_truenas_still_shows_docker(run_host, server):
    def unreachable(url, *, verify, timeout):
        raise HopsError(f"cannot reach TrueNAS at {url}: connection refused")

    server.connect = unreachable
    code, out, _ = run_host("--json")
    report = json.loads(out)
    assert code == 0
    assert len(report["docker"]) == 4 and report["pools"] is None and report["tasks"] is None
    assert report["notes"] == ["pools and the sections after it: TrueNAS unreachable"]


@pytest.mark.parametrize("method", ["cloudsync.query", "pool.snapshottask.query"])
def test_a_failed_read_skips_only_its_section_without_echoing_the_error(run_host, server, method):
    def failing(*params):
        raise FakeTrueNASError("EFAULT", f"rclone --b2-key {SECRET} failed; pre_script: echo {SECRET}")

    server.methods[method] = failing
    for argv in ((), ("--json",)):
        code, out, err = run_host(*argv)
        assert code == 0
        assert SECRET not in out + err
    report = json.loads(out)
    assert report["tasks"] is None and report["pools"] and report["rollback_snapshots"]
    assert report["notes"] == [f"tasks: TrueNAS read failed ({method})"]


def test_dataset_errors_are_noted_without_their_text(run_host, server):
    def failing(*params):
        raise FakeTrueNASError("EINVAL", f"bad path {SECRET}")

    server.methods["filesystem.statfs"] = failing
    server.methods["zfs.resource.query"] = failing
    code, out, err = run_host("--json", env={"HOPS_HOST_DATASETS": "luna"})
    assert code == 0 and SECRET not in out + err
    assert json.loads(out)["notes"] == [
        "docker dataset unknown: TrueNAS read failed (filesystem.statfs)",
        "restic dataset unknown: TrueNAS read failed (filesystem.statfs)",
        "dataset luna skipped: TrueNAS read failed (zfs.resource.query)"]


def test_restic_dataset_is_only_the_repository_mount(run_host, host):
    stack_back = container("stack-back", project="ops", service="stack-back")
    stack_back["Mounts"] = [
        {"Type": "bind", "Source": "/mnt/andromeda/secrets/b2.env", "Destination": "/run/secrets/b2.env"},
        *STACK_BACK_MOUNTS]
    host.respond("inspect", "stack-back", stdout=json.dumps([stack_back]))
    report = json.loads(run_host("--json")[1])
    assert [(d["name"], d["roles"]) for d in report["datasets"]] == [
        ("luna/ix-apps/docker", ["docker"]), ("andromeda/docker-backups", ["restic"])]
