"""Read tools/validate/paths as data: one `NAME=PATH` homelab-validate input per line, each becoming one `--path`.
Read tools/validate/privacy-allow beside it the same way: one regex per line, each becoming one `--privacy-allow`.

Run as a script, it prints the arguments shell-quoted (`shlex.join`) for the reusable workflow's `extra-args`,
which splits them with `shlex.split`. Any malformed line fails, so neither file can inject an option.
"""
import re
import shlex
import sys
from pathlib import Path

PATHS = Path(__file__).resolve().parent / "paths"
ALLOW = "privacy-allow"
# homelab-validate's inputs (validate-v1.4.0 README, "Inputs").
NAMES = {
    "prometheus", "rule-tests", "alertmanager", "loki", "alloy", "grafana", "dashboards-json", "dashboards",
    "declared-containers", "blackbox", "traefik",
}
LINE = re.compile(r"[a-z][a-z0-9-]*=[^\s]+")


class PathsError(ValueError):
    pass


def load(path: Path = PATHS) -> list[str]:
    args: list[str] = []
    seen: set[str] = set()
    for n, line in enumerate(Path(path).read_text().split("\n"), 1):
        if not line.strip() or line.startswith("#"):
            continue
        if not LINE.fullmatch(line):
            raise PathsError(f"{path}: line {n}: want NAME=PATH with no whitespace: {line!r}")
        name, value = line.split("=", 1)
        if name not in NAMES:
            raise PathsError(f"{path}: line {n}: unknown input {name!r} (known: {', '.join(sorted(NAMES))})")
        if value.startswith("-"):
            raise PathsError(f"{path}: line {n}: path must not start with '-': {line!r}")
        if name in seen:
            raise PathsError(f"{path}: line {n}: {name!r} given twice")
        seen.add(name)
        args += ["--path", line]
    return args


def load_allow(path: Path) -> list[str]:
    """The privacy allowlist beside the paths file, as `--privacy-allow` arguments; none when the file is absent."""
    path = Path(path)
    if not path.exists():
        return []
    args: list[str] = []
    for n, line in enumerate(path.read_text().split("\n"), 1):
        if not line.strip() or line.startswith("#"):
            continue
        if line != line.strip():
            raise PathsError(f"{path}: line {n}: want a regex with no leading or trailing whitespace")
        try:
            re.compile(line)
        except re.error as exc:
            raise PathsError(f"{path}: line {n}: not a valid regex: {exc}") from None
        args += ["--privacy-allow", line]
    return args


def main(argv: list[str]) -> int:
    paths = Path(argv[0]) if argv else PATHS
    try:
        args = load(paths) + load_allow(paths.with_name(ALLOW))
    except (OSError, PathsError) as exc:
        print(f"validate_paths: {exc}", file=sys.stderr)
        return 1
    print(shlex.join(args))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
