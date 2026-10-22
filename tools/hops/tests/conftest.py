"""Shared fixtures for hops tests: a fixture repository and a fake Host that records SSH argv.

CI runs `python3 -m pytest tools host` without installing hops, so this puts the package on sys.path.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hops_fakes import STACKS, FakeHost, write  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "live: read-only checks against the real Host; run with -m live")


def pytest_collection_modifyitems(config, items):
    if "live" in (config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(reason="talks to the real Host; run with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A home-ops checkout with the STACKS fixture (not a git repository)."""
    root = tmp_path / "repo"
    write(root, STACKS)
    return root


@pytest.fixture
def host_config(tmp_path):
    from hops.config import HostConfig

    return HostConfig(host="tn.example", user="agent", key=Path("/keys/id"), control_dir=tmp_path / "cm")


@pytest.fixture
def fake_host() -> FakeHost:
    return FakeHost()


@pytest.fixture
def runner(host_config, fake_host):
    """A real Runner whose ssh process is fake_host."""
    from hops.runner import Runner

    return Runner(host_config, process=fake_host)


@dataclass
class Outcome:
    code: int
    out: str
    err: str


@pytest.fixture
def hops(repo, runner, capsys):
    """Run the hops CLI against the fixture repository and fake Host: hops("status", "--json")."""
    from hops import cli

    def run(*argv: str) -> Outcome:
        ctx = cli.Context(repo_root=repo, runner=runner)
        code = cli.main(list(argv), ctx=ctx)
        captured = capsys.readouterr()
        return Outcome(code, captured.out, captured.err)

    return run
