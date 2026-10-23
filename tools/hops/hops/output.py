"""Printing: JSON for agents (--json), aligned tables for people."""
import json
import sys


def print_json(data, file=None) -> None:
    """One JSON document. Keys keep the order the command builds them in (documented in README)."""
    (file or sys.stdout).write(json.dumps(data, indent=2) + "\n")


def table(headers: list[str], rows: list[list], file=None) -> None:
    cells = [[str("-" if v is None else v) for v in row] for row in [headers, *rows]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    for row in cells:
        (file or sys.stdout).write("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() + "\n")


def add_json_flag(parser) -> None:
    parser.add_argument("--json", action="store_true", help="print JSON (schema in tools/hops/README.md)")


def size(n: int | None) -> str | None:
    """Bytes in binary units, e.g. 6.1GiB."""
    if n is None:
        return None
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"
