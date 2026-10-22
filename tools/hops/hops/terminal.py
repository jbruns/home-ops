"""The local terminal: interactive (Disruptive) modes need a person at a TTY."""
import sys

from hops.errors import UsageError


def stdin_is_tty() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def require_tty(what: str) -> None:
    """Refuse an interactive mode when stdin is not a TTY, e.g. when an agent runs hops."""
    if not stdin_is_tty():
        raise UsageError(f"{what} is interactive and needs a TTY on stdin")
