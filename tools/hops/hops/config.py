"""Where hops finds the repo and how it reaches the Host.

Every site-specific value comes from here, so a second Host only needs different config:

- repo root: `git rev-parse --show-toplevel` of the working directory when that checkout has a
  Stack root; otherwise the checkout hops is installed from (`uv tool install -e`).
- `.env`: the repo root's, else the main worktree's (agent worktrees under `.worktrees/` have none).
- environment variables override `.env`.
"""
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hops.errors import ConfigError

INSTALL_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = {
    "HOPS_SSH_USER": "homeops-agent",
    "HOPS_SSH_KEY": "~/.ssh/homeops_agent_ed25519",
    "HOPS_STACK_ROOT": "tn",
}


@dataclass(frozen=True)
class HostConfig:
    host: str
    user: str
    key: Path
    control_dir: Path  # ssh ControlPath sockets


def _git(cwd: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.strip() or None


def stack_root_name(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("HOPS_STACK_ROOT") or DEFAULTS["HOPS_STACK_ROOT"]


def find_repo_root(cwd: Path | None = None, env=None) -> Path:
    """The home-ops checkout to read declared state from."""
    stacks = stack_root_name(env)
    top = _git(Path(cwd or Path.cwd()), "rev-parse", "--show-toplevel")
    if top and (Path(top) / stacks).is_dir():
        return Path(top).resolve()
    if (INSTALL_ROOT / stacks).is_dir():
        return INSTALL_ROOT
    raise ConfigError(f"no home-ops checkout found: run hops inside one (it has {stacks}/)")


def env_files(repo_root: Path) -> list[Path]:
    """The .env files hops reads, first match wins: the checkout's, then the main worktree's."""
    candidates = [repo_root / ".env"]
    common = _git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        candidates.append(Path(common).parent / ".env")
    return list(dict.fromkeys(p.resolve() for p in candidates))


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines, comments, `export ` and surrounding quotes are allowed."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_host(repo_root: Path, env=None) -> HostConfig:
    env = dict(os.environ if env is None else env)
    files = env_files(repo_root)
    found = next((f for f in files if f.is_file()), None)
    values = {**DEFAULTS, **(read_env_file(found) if found else {}), **env}
    if not values.get("TRUENAS_HOST"):
        raise ConfigError("missing TRUENAS_HOST: set it in the environment or in "
                          + " or ".join(str(f) for f in files))
    home = values.get("HOME") or str(Path.home())
    cache = values.get("XDG_CACHE_HOME") or f"{home}/.cache"
    return HostConfig(
        host=values["TRUENAS_HOST"],
        user=values["HOPS_SSH_USER"],
        key=Path(_expand(values["HOPS_SSH_KEY"], home)),
        control_dir=Path(cache) / "hops",
    )


def _expand(path: str, home: str) -> str:
    return home + path[1:] if path == "~" or path.startswith("~/") else path
