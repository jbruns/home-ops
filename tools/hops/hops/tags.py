"""Image tag shapes: which tags count as newer releases of a pinned tag.

The same rule as homelab-components' Canary: a candidate has the pinned tag's `v` prefix, number
of version parts and variant suffix (`-alpine`), so a pre-release (`-rc.1`) or another variant
never qualifies.
"""
import re

VERSION_TAG = re.compile(r"^(?P<prefix>v?)(?P<numbers>\d+(?:\.\d+)*)(?P<suffix>-[A-Za-z][\w.]*)?$")


def _shape(tag: str):
    m = VERSION_TAG.match(tag)
    if not m:
        return None
    numbers = tuple(int(n) for n in m["numbers"].split("."))
    return (m["prefix"], len(numbers), m["suffix"] or ""), numbers


def newest_same_shape(pinned: str, tags) -> str | None:
    """The newest of `tags` shaped like `pinned`, or None when `pinned` is not a version tag."""
    parsed = _shape(pinned)
    if parsed is None:
        return None
    shape = parsed[0]
    candidates = [(p[1], tag) for tag in tags if (p := _shape(tag)) and p[0] == shape]
    return max(candidates)[1] if candidates else None
