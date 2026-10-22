"""Config: repo root, the repo-root .env, and environment overrides."""
from pathlib import Path

import pytest
from hops_fakes import git, write

from hops import config
from hops.errors import ConfigError

TOOL_DIR = Path(__file__).resolve().parents[1]


def test_host_config_reads_dotenv_with_defaults(tmp_path):
    write(tmp_path, {".env": """
        # Host access
        TRUENAS_URL=https://tn.example
        export TRUENAS_HOST="tn.example"
    """})
    host = config.load_host(tmp_path, env={"HOME": "/home/me"})
    assert host.host == "tn.example"
    assert host.user == "homeops-agent"
    assert host.key == Path("/home/me/.ssh/homeops_agent_ed25519")


def test_environment_overrides_dotenv(tmp_path):
    write(tmp_path, {".env": "TRUENAS_HOST=tn.example\nHOPS_SSH_USER=someone\n"})
    host = config.load_host(tmp_path, env={
        "HOME": "/home/me", "TRUENAS_HOST": "other.example", "HOPS_SSH_KEY": "~/.ssh/k",
    })
    assert (host.host, host.user, host.key) == ("other.example", "someone", Path("/home/me/.ssh/k"))


def test_missing_host_names_the_key_and_where_it_looked(tmp_path):
    with pytest.raises(ConfigError, match=r"TRUENAS_HOST.*\.env"):
        config.load_host(tmp_path, env={"HOME": "/home/me"})


def test_control_socket_lives_in_the_user_cache_dir(tmp_path):
    host = config.load_host(tmp_path, env={"HOME": "/home/me", "TRUENAS_HOST": "h", "XDG_CACHE_HOME": "/c"})
    assert host.control_dir == Path("/c/hops")
    host = config.load_host(tmp_path, env={"HOME": "/home/me", "TRUENAS_HOST": "h"})
    assert host.control_dir == Path("/home/me/.cache/hops")


def test_dotenv_falls_back_to_the_main_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    write(main, {"tn/edge/compose.yaml": "services: {}\n", ".env": "TRUENAS_HOST=tn.example\n"})
    git(main, "add", "tn")
    git(main, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
    git(main, "worktree", "add", "-q", str(main / ".worktrees" / "1"))
    worktree = main / ".worktrees" / "1"

    assert config.find_repo_root(worktree / "tn", env={}) == worktree.resolve()
    assert config.load_host(worktree, env={"HOME": "/h"}).host == "tn.example"


def test_repo_root_is_the_git_toplevel_of_cwd(repo):
    git(repo, "init", "-q")
    (repo / "tn" / "edge").mkdir(exist_ok=True)
    assert config.find_repo_root(repo / "tn" / "edge", env={}) == repo.resolve()


def test_outside_a_checkout_uses_the_one_hops_is_installed_from(tmp_path):
    assert config.find_repo_root(tmp_path, env={}) == TOOL_DIR.parents[1]
