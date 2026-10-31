"""Characterization tests for the Grafana dashboard generator."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOL_DIR.parents[1]
sys.path.insert(0, str(TOOL_DIR))

import dashboards  # noqa: E402

NAMES = ["overview.json", "service.json", "machine.json", "dependencies.json", "plex.json", "arr.json", "dispatcharr.json"]


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL_DIR / "dashboards.py"), *args], capture_output=True, text=True)


def test_render_is_deterministic_json():
    files = dashboards.render()
    assert list(files) == NAMES
    assert files == dashboards.render()
    for text in files.values():
        doc = json.loads(text)
        assert doc["uid"] and doc["panels"] and text.endswith("\n")


def test_default_output_is_the_provisioned_folder():
    assert dashboards.OUT == REPO_ROOT / "tn/monitoring/grafana/dashboards"


def test_committed_json_is_current():
    r = run("--check")
    assert r.returncode == 0, r.stdout


@pytest.fixture
def out(tmp_path):
    return tmp_path / "dashboards"


def test_write_then_check(out):
    assert run("--out", str(out)).returncode == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(NAMES)
    assert run("--out", str(out), "--check").returncode == 0


def test_check_reports_stale_missing_and_extra(out, capsys):
    dashboards.main(["--out", str(out)])
    (out / "plex.json").write_text("{}\n")
    (out / "arr.json").unlink()
    (out / "hand-made.json").write_text("{}\n")
    with pytest.raises(SystemExit) as e:
        dashboards.main(["--out", str(out), "--check"])
    assert e.value.code == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{out / 'plex.json'}: stale; run python3 tools/dashboards/dashboards.py",
        f"{out / 'arr.json'}: stale; run python3 tools/dashboards/dashboards.py",
        f"{out / 'hand-made.json'}: not produced by the generator",
    ]
