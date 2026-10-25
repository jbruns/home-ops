"""Tests for validate_paths.py and the homelab-validate wrapper that uses it."""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import validate_paths  # noqa: E402

BAD_LINES = [
    "prometheus=tn/monitoring/prometheus --help",
    "prometheus=--help",
    "--help=tn/monitoring/prometheus",
    "nonsense=tn/monitoring/prometheus",
    "prometheus",
    "prometheus=",
    "prometheus=a\tb",
]


def write(tmp_path, *lines):
    f = tmp_path / "paths"
    f.write_text("# comment\n\n" + "".join(f"{line}\n" for line in lines))
    return f


def test_each_line_is_one_path_value(tmp_path):
    f = write(tmp_path, "prometheus=tn/monitoring/prometheus", "traefik=tn/edge/traefik/service.yaml")
    assert validate_paths.load(f) == [
        "--path", "prometheus=tn/monitoring/prometheus", "--path", "traefik=tn/edge/traefik/service.yaml",
    ]


def test_the_repo_file_is_valid():
    assert validate_paths.load(TOOL_DIR / "paths")


@pytest.mark.parametrize("line", BAD_LINES)
def test_a_bad_line_is_refused(tmp_path, line):
    with pytest.raises(validate_paths.PathsError, match="line 3"):
        validate_paths.load(write(tmp_path, line))


def test_a_repeated_name_is_refused(tmp_path):
    with pytest.raises(validate_paths.PathsError, match="prometheus"):
        validate_paths.load(write(tmp_path, "prometheus=a", "prometheus=b"))


def test_ci_output_round_trips_through_shlex_split(tmp_path):
    f = write(tmp_path, "prometheus=tn/monitoring/prometheus", "dashboards=tools/dashboards/dashboards.py")
    out = subprocess.run([sys.executable, TOOL_DIR / "validate_paths.py", f], capture_output=True, text=True,
                         check=True).stdout
    assert shlex.split(out) == validate_paths.load(f)


@pytest.mark.parametrize("line", BAD_LINES)
def test_ci_output_fails_on_a_bad_line(tmp_path, line):
    r = subprocess.run([sys.executable, TOOL_DIR / "validate_paths.py", write(tmp_path, line)],
                       capture_output=True, text=True)
    assert r.returncode != 0 and r.stdout == "" and "line 3" in r.stderr


@pytest.mark.parametrize("line", BAD_LINES)
def test_wrapper_fails_on_a_bad_line_without_running_homelab_validate(tmp_path, line):
    repo = tmp_path / "repo"
    (repo / "tools/validate").mkdir(parents=True)
    for name in ("homelab-validate", "validate_paths.py"):
        shutil.copy2(TOOL_DIR / name, repo / "tools/validate" / name)
    write(repo / "tools/validate", line)
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "homelab-validate").write_text("#!/bin/sh\necho ran\n")
    (stub / "homelab-validate").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}
    r = subprocess.run([repo / "tools/validate/homelab-validate", "secrets"], capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "ran" not in r.stdout and "line 3" in r.stderr


def test_wrapper_passes_each_line_as_one_argument(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tools/validate").mkdir(parents=True)
    for name in ("homelab-validate", "validate_paths.py"):
        shutil.copy2(TOOL_DIR / name, repo / "tools/validate" / name)
    write(repo / "tools/validate", "prometheus=tn/monitoring/prometheus")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "homelab-validate").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    (stub / "homelab-validate").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}
    r = subprocess.run([repo / "tools/validate/homelab-validate", "secrets"], capture_output=True, text=True,
                       env=env, check=True)
    assert r.stdout.splitlines() == ["--root", str(repo.resolve()), "--path", "prometheus=tn/monitoring/prometheus", "secrets"]


def write_allow(directory, *lines):
    f = directory / validate_paths.ALLOW
    f.write_text("# comment\n\n" + "".join(f"{line}\n" for line in lines))
    return f


def test_each_allow_line_is_one_privacy_allow_value(tmp_path):
    f = write_allow(tmp_path, r"git\+https://\S+", '^"x y"$')
    assert validate_paths.load_allow(f) == ["--privacy-allow", r"git\+https://\S+", "--privacy-allow", '^"x y"$']


def test_a_missing_allowlist_adds_nothing(tmp_path):
    assert validate_paths.load_allow(tmp_path / validate_paths.ALLOW) == []


def test_the_repo_allowlist_is_valid():
    assert validate_paths.load_allow(TOOL_DIR / validate_paths.ALLOW)


@pytest.mark.parametrize("line", ["(unclosed", " leading", "trailing "])
def test_a_bad_allow_line_is_refused(tmp_path, line):
    with pytest.raises(validate_paths.PathsError, match="line 3"):
        validate_paths.load_allow(write_allow(tmp_path, line))


def test_ci_output_carries_the_allowlist_beside_the_paths(tmp_path):
    f = write(tmp_path, "prometheus=tn/monitoring/prometheus")
    write_allow(tmp_path, '^"a b"$')
    out = subprocess.run([sys.executable, TOOL_DIR / "validate_paths.py", f], capture_output=True, text=True,
                         check=True).stdout
    assert shlex.split(out) == ["--path", "prometheus=tn/monitoring/prometheus", "--privacy-allow", '^"a b"$']
