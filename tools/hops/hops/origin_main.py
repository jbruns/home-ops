"""Declared state as origin/main has it: what doco-cd deploys, whatever this checkout holds."""
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

from hops.declared import Declared
from hops.errors import ConfigError, HopsError

REF = "origin/main"


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise HopsError(f"git {' '.join(args)} failed: {detail.decode(errors='replace').strip() or exc}") from None


def load(repo_root: Path, stack_root: str = "tn") -> tuple[Declared, str]:
    """Fetch main from origin and load the Stack root of origin/main; returns it and the commit.

    The tree is unpacked under the repository's git directory, so it never mixes with the checkout.
    Refuses an inventory without Services: removal must never run against a partial declared state.
    """
    _git(repo_root, "fetch", "--quiet", "origin", "main")
    commit = _git(repo_root, "rev-parse", "--verify", f"{REF}^{{commit}}").decode().strip()
    common = Path(_git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
    dest = common / "hops" / "origin-main" / commit
    for stale in dest.parent.glob("*"):  # earlier commits' trees, and a half-written one
        shutil.rmtree(stale, ignore_errors=True)
    dest.mkdir(parents=True)
    archive = _git(repo_root, "archive", "--format=tar", commit, "--", stack_root)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # Python before 3.11.4; git archive of our own commit is trusted
            tar.extractall(dest)
    declared = Declared.load(dest, stack_root)
    if not declared.services:
        raise ConfigError(f"{REF} ({commit[:12]}) declares no Services under {stack_root}/; refusing to remove "
                          "anything against it")
    return declared, commit
