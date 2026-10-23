"""Moving a volume's contents into a new volume, for a rename (ADR 0010). Docker cannot rename a volume.

One throwaway container bind-mounts Docker's volumes directory once, so each top-level entry of
<old>/_data moves to <new>/_data with rename(2) within one filesystem: instant, and ownership,
permissions and timestamps stay as they are. The new _data
directory itself takes the old one's owner and mode. Symlinks are renamed, never followed. The image is the
python one hops already runs for `logs --loki` (hops/loki.py), never pulled.

SCRIPT runs in that container (or, in tests, on this machine against a directory). It prints one
JSON document: the filesystem mounted at ROOT and, for each volume, its _data's device and number of
top-level entries. `check` only reads; `move` re-checks, renames, and reports the state either way.
"""
import json
import re
from dataclasses import dataclass

from hops import docker, loki
from hops.declared import Declared
from hops.docker import Container
from hops.errors import HopsError, UsageError
from hops.runner import Runner

# Docker's rule for volume names (daemon/names.RestrictedNamePattern). Checked before any Host call,
# so a name is never more than a plain argument.
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+")
ROOT = "/volumes"  # where the container sees Docker's volumes directory
TIMEOUT = 600
LABEL = "hops.volume-move"
SCRIPT = r"""
import json, os, sys

mode, root, old, new = sys.argv[1:5]


def mount():
    # The filesystem mounted at root: [type, source]; for ZFS the source is the dataset.
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                if left.split()[4] == root:
                    return right.split()[:2]
    except OSError:
        pass
    return None


def volume(name):
    data = os.path.join(root, name, "_data")
    if os.path.islink(data) or not os.path.isdir(data):
        return None
    return {"dev": os.lstat(data).st_dev, "entries": len(os.listdir(data))}


def state(**extra):
    return {"mount": mount(), "dev": os.stat(root).st_dev, "old": volume(old), "new": volume(new), **extra}


if mode == "check":
    print(json.dumps(state()))
    sys.exit(0)

moved = 0
try:
    before = state()
    if not before["old"] or not before["new"]:
        raise RuntimeError("both volumes' _data must be directories under " + root)
    if not before["old"]["dev"] == before["new"]["dev"] == before["dev"]:
        raise RuntimeError("the volumes are not on one filesystem, so rename(2) cannot move them")
    if before["new"]["entries"]:
        raise RuntimeError(new + " is not empty")
    src, dst = os.path.join(root, old, "_data"), os.path.join(root, new, "_data")
    # The new _data takes the old one's owner and mode: a Datastore's often belongs to its user.
    st = os.lstat(src)
    os.chown(dst, st.st_uid, st.st_gid)
    os.chmod(dst, st.st_mode & 0o7777)
    for entry in os.listdir(src):
        if os.path.lexists(os.path.join(dst, entry)):
            raise RuntimeError("an entry already exists in " + new)
        os.rename(os.path.join(src, entry), os.path.join(dst, entry))
        moved += 1
except Exception as exc:
    print(json.dumps(state(moved=moved, error=f"{type(exc).__name__}: {exc}")))
    sys.exit(1)
print(json.dumps(state(moved=moved, error=None)))
"""


@dataclass(frozen=True)
class Probe:
    """What SCRIPT saw: the filesystem under the volumes directory, and each volume's _data."""
    fstype: str | None
    source: str | None  # the ZFS dataset holding the volumes directory
    dev: int
    old: dict | None  # {"dev", "entries"}; None when <old>/_data is missing
    new: dict | None
    moved: int | None = None
    error: str | None = None

    @classmethod
    def parse(cls, text: str) -> "Probe":
        try:
            data = json.loads(text)
            fstype, source = data["mount"] or (None, None)
            return cls(fstype, source, data["dev"], data["old"], data["new"], data.get("moved"), data.get("error"))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise HopsError(f"the volume-move container printed something other than its report: {text[:200]!r}") \
                from None


def check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        raise UsageError(f"{name!r} is not a Docker volume name ([a-zA-Z0-9][a-zA-Z0-9_.-]+)")


def convention(declared: Declared, name: str) -> str | None:
    """A warning when `name` does not read <stack>_[<service>_]<purpose> for a declared Stack (ADR 0010)."""
    stack, sep, rest = name.partition("_")
    if sep and rest and stack in declared.stacks:
        return None
    return f"{name} does not follow ADR 0010's <stack>_[<service>_]<purpose> for a declared Stack"


def volumes_dir(runner: Runner) -> str:
    """Docker's volumes directory on the Host: <DockerRootDir>/volumes, from `docker info`."""
    root = json.loads(runner.docker("info", "--format", "{{json .DockerRootDir}}").stdout or "null")
    if not isinstance(root, str) or not root.startswith("/"):
        raise HopsError(f"docker info gave no DockerRootDir: {root!r}")
    return root.rstrip("/") + "/volumes"


def inspect(runner: Runner, name: str) -> dict | None:
    result = runner.docker("volume", "inspect", name, check=False)
    if result.returncode != 0:
        if "no such volume" in result.stderr.lower():
            return None
        raise HopsError(f"docker volume inspect {name} failed: {result.stderr.strip()}")
    found = json.loads(result.stdout or "[]")
    return found[0] if found else None


def local_problem(volume: dict, directory: str) -> str | None:
    """Why `volume`'s data is not plain files under Docker's volumes directory, or None."""
    name = volume["Name"]
    if volume.get("Driver") != "local":
        return f"its driver is {volume.get('Driver')}, not local"
    if volume.get("Options"):
        opts = ", ".join(f"{k}={v}" for k, v in sorted(volume["Options"].items()))
        return f"it has driver options ({opts}), so its data is not in {directory}/{name}/_data"
    if volume.get("Mountpoint") != f"{directory}/{name}/_data":
        return f"its Mountpoint {volume.get('Mountpoint')} is not {directory}/{name}/_data"
    return None


def run(runner: Runner, image: str, directory: str, mode: str, old: str, new: str):
    """SCRIPT in a throwaway container that mounts `directory` at ROOT (read-only for `check`)."""
    return runner.docker(
        "run", "--rm", "--label", "startup-order=skip", "--label", f"{LABEL}={old}",
        "--log-driver", "none", "--network", "none", "--pull", "never", "--read-only",
        "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
        "--cap-add", "FOWNER",
        "--memory", "256m", "-v", f"{directory}:{ROOT}{':ro' if mode == 'check' else ''}",
        image, "python3", "-c", SCRIPT, mode, ROOT, old, new, check=False, timeout=TIMEOUT)


def check(runner: Runner, image: str, directory: str, old: str, new: str) -> Probe:
    result = run(runner, image, directory, "check", old, new)
    if result.returncode != 0:
        raise HopsError(f"the volume-move container could not read {directory}: "
                        f"{result.stderr.strip() or f'exit code {result.returncode}'}")
    return Probe.parse(result.stdout)


def move(runner: Runner, image: str, directory: str, old: str, new: str) -> Probe:
    """Rename every top-level entry of <old>/_data into <new>/_data. The Probe carries `error` when it
    stopped part-way; a HopsError means no report came back at all."""
    result = run(runner, image, directory, "move", old, new)
    if not result.stdout.strip():
        raise HopsError(f"the volume-move container gave no report: "
                        f"{result.stderr.strip() or f'exit code {result.returncode}'}")
    return Probe.parse(result.stdout.strip().splitlines()[-1])


def image(declared: Declared) -> str:
    """The python image the monitoring Stack pins (as for logs --loki), present on the Host."""
    service = declared.get(*loki.TRANSPORT)
    if not service or not service.image:
        raise HopsError(f"{'/'.join(loki.TRANSPORT)} is not declared with a pinned image; "
                        "hops volume move runs its image")
    return service.image.ref


def users(runner: Runner, name: str) -> list[Container]:
    """Every container, running or stopped, that mounts the volume `name`."""
    ids = runner.docker("ps", "-aq", "--no-trunc", "--filter", f"volume={name}").stdout.split()
    return [c for c in docker.inspect(runner, *ids)
            if any(m.get("Type") == "volume" and m.get("Name") == name for m in c.raw.get("Mounts") or [])]
