"""Image tag shapes: which tags count as newer releases of a pinned tag.

The same rule as homelab-components' Canary: a candidate has the pinned tag's `v` prefix, number
of version parts and variant suffix (`-alpine`), so a pre-release (`-rc.1`) or another variant
never qualifies. A candidate more than one major version ahead of the pin is rejected: registries
carry stray tags (lscr.io's qbittorrent has a 2021 `20.04.1` beside `5.2.4`).
"""
import re
from typing import NamedTuple

VERSION_TAG = re.compile(r"^(?P<prefix>v?)(?P<numbers>\d+(?:\.\d+)*)(?P<suffix>-[A-Za-z][\w.]*)?$")


def _shape(tag: str):
    m = VERSION_TAG.match(tag)
    if not m:
        return None
    numbers = tuple(int(n) for n in m["numbers"].split("."))
    return (m["prefix"], len(numbers), m["suffix"] or ""), numbers


class Proposal(NamedTuple):
    tag: str | None  # the newest acceptable tag; the pin itself when nothing newer qualifies
    rejected: str | None  # the newest same-shape tag, when it jumps more than one major version


def propose(pinned: str, tags) -> Proposal:
    """The newest of `tags` shaped like `pinned`. The pin is always a candidate, so a registry that
    lacks it never yields a downgrade. Proposal(None, None) when `pinned` is not a version tag."""
    parsed = _shape(pinned)
    if parsed is None:
        return Proposal(None, None)
    shape, numbers = parsed
    candidates = sorted({(p[1], tag) for tag in [*tags, pinned] if (p := _shape(tag)) and p[0] == shape},
                        reverse=True)
    acceptable = [tag for version, tag in candidates if version[0] <= numbers[0] + 1]
    newest = candidates[0][1]
    return Proposal(acceptable[0], None if newest == acceptable[0] else newest)
