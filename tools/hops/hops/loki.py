"""Loki's query_range, asked from a throwaway container on the Host.

Loki listens only on the monitoring Stack's default network and has no auth (auth_enabled: false),
so no credential is involved. Each request is one `docker run --rm` of the image the monitoring
Stack already pins for dependencies-render (python), never pulled. The container's own output is
the Loki response, so it runs with `--log-driver none`: otherwise Alloy would ship it back to Loki.

Selectors use the `container` label Alloy sets (tn/monitoring/alloy/config.alloy). A container_name
is the same across recreations and across a Stack's Compose project being renamed (the `stack`
label holds legacy project names such as `arr`); a Service without one matches every
<stack>-<service>-<n> Compose has given it.
"""
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlencode

from hops.declared import Declared, Service
from hops.errors import HopsError, UsageError
from hops.runner import Runner

ADDRESS = "loki:3100"
NETWORK = "monitoring_default"
TRANSPORT = ("monitoring", "dependencies-render")  # a Service whose image has python3
# Lines per request. Loki returns at most MAX_ENTRIES (its max_entries_limit_per_query default)
# per request; staying below it lets one request fetch every line tied at a page's last timestamp.
PAGE = 4000
MAX_ENTRIES = 5000
TIMEOUT = 60
# Docker's two streams; Alloy also ships Traefik's access log as logstream=access.
STREAMS = 'logstream=~"stdout|stderr"'
SCRIPT = """\
import sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=float(sys.argv[2])) as r:
        sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as e:
    sys.stderr.write(f"HTTP {e.code}: {e.read().decode(errors='replace').strip()}")
    sys.exit(3)
except OSError as e:
    sys.stderr.write(f"cannot reach Loki: {e}")
    sys.exit(4)
"""
DURATION = re.compile(r"(?:\d+[smhdw])+")
DURATION_PART = re.compile(r"(\d+)([smhdw])")
# ISO 8601 with up to 9 fractional digits, which datetime would cut to 6.
FRACTION = re.compile(r"(?P<head>[^.]*?)(?:\.(?P<frac>\d{1,9}))?(?P<tz>Z|[+-]\d{2}:?\d{2})?")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
REGEX_SPECIAL = re.compile(r"([.^$*+?()\[\]{}|\\])")


def literal(text: str) -> str:
    """`text` as an RE2 pattern that matches only itself."""
    return REGEX_SPECIAL.sub(r"\\\1", text)


def selector(services: list[Service]) -> str:
    """The LogQL stream selector for the containers of `services` (Docker's streams only)."""
    # Compose names a container without container_name <project>-<service>-<n>.
    patterns = [literal(s.container_name) if s.container_name else literal(f"{s.stack}-{s.name}-") + "[0-9]+"
                for s in services]
    if len(services) == 1 and services[0].container_name:
        matcher = f"container={json.dumps(services[0].container_name)}"
    else:
        matcher = f"container=~{json.dumps('|'.join(patterns))}"
    return f"{{{matcher}, {STREAMS}}}"


def with_grep(query: str, grep: str | None) -> str:
    # LogQL decodes \\u escapes but not surrogate pairs, so characters stay as they are.
    return f"{query} |~ {json.dumps(grep, ensure_ascii=False)}" if grep else query


def parse_time(value: str, now: datetime) -> int:
    """Nanoseconds since the epoch for `value`: a duration back from now (90s, 2h, 1h30m, 7d) or
    an ISO 8601 time (UTC unless it carries an offset)."""
    if DURATION.fullmatch(value):
        seconds = sum(int(n) * UNITS[u] for n, u in DURATION_PART.findall(value))
        return to_ns(now) - seconds * 10**9
    parts = FRACTION.fullmatch(value)
    try:
        if not parts:
            raise ValueError
        when = datetime.fromisoformat(parts["head"] + (parts["tz"] or ""))
    except ValueError:
        raise UsageError(f"{value!r} is neither a duration (10m, 2h, 7d) nor an ISO 8601 time") from None
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return int(when.timestamp()) * 10**9 + int((parts["frac"] or "").ljust(9, "0"))


def to_ns(when: datetime) -> int:
    return int(when.timestamp()) * 10**9 + when.microsecond * 1000


def rfc3339(ns: int) -> str:
    """Docker's --timestamps form: UTC with nanoseconds."""
    seconds, frac = divmod(ns, 10**9)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{frac:09d}Z"


def transport_image(declared: Declared) -> str:
    service = declared.get(*TRANSPORT)
    if not service or not service.image:
        raise HopsError(f"{'/'.join(TRANSPORT)} is not declared with a pinned image; "
                        "hops logs --loki runs its image to reach Loki")
    return service.image.ref


def fetcher(runner: Runner, image: str) -> Callable[[dict], dict]:
    """A function that sends one query_range request from a throwaway container on the Host."""
    def fetch(params: dict) -> dict:
        url = f"http://{ADDRESS}/loki/api/v1/query_range?{urlencode(params)}"
        result = runner.docker(
            "run", "--rm", "--label", "startup-order=skip", "--log-driver", "none",
            "--network", NETWORK, "--pull", "never", "--read-only", "--cap-drop", "ALL",
            "--user", "65534:65534", "--memory", "256m",
            image, "python3", "-c", SCRIPT, url, str(TIMEOUT), check=False, timeout=TIMEOUT + 30)
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            raise HopsError(f"Loki query failed: {detail}")
        try:
            data = json.loads(result.stdout)["data"]
        except (json.JSONDecodeError, KeyError, TypeError):
            raise HopsError(f"Loki answered something other than JSON: {result.stdout[:200]!r}") from None
        if data.get("resultType") != "streams":
            raise HopsError(f"the query returned {data.get('resultType')}, not log lines; "
                            "hops logs --loki takes log queries only")
        return data
    return fetch


def entries(data: dict) -> list[tuple[int, dict, str]]:
    """(ns, labels, line) for every value in a streams answer, oldest first."""
    found = [(int(ns), s.get("stream") or {}, line)
             for s in data.get("result") or [] for ns, line, *_ in s.get("values") or []]
    return sorted(found, key=lambda e: e[0])


def query_range(fetch: Callable[[dict], dict], query: str, start: int, end: int,
                limit: int) -> tuple[list[tuple[int, dict, str]], bool]:
    """Up to `limit` entries in [start, end), oldest first, paging PAGE lines at a time.
    Returns them and whether Loki may hold more in the range.

    Each page starts at the last page's newest timestamp, since lines can share one; the lines
    already seen at that timestamp are dropped. When a whole page is lines already seen at that
    timestamp, one request of up to MAX_ENTRIES fetches every line at it, and paging goes on
    after it."""
    found: list[tuple[int, dict, str]] = []
    seen_at_start: set = set()
    while len(found) < limit:
        want = min(PAGE, limit - len(found) + len(seen_at_start))
        page = entries(fetch({"query": query, "start": start, "end": end, "limit": want,
                              "direction": "forward"}))
        new = [e for e in page if key(e) not in seen_at_start][:limit - len(found)]
        found += new
        if len(page) < want:
            return found, False
        if not new:
            tied = entries(fetch({"query": query, "start": start, "end": start + 1, "limit": MAX_ENTRIES,
                                  "direction": "forward"}))
            if len(tied) >= MAX_ENTRIES:
                raise HopsError(f"{MAX_ENTRIES} or more lines share the timestamp {rfc3339(start)}, more than "
                                "Loki returns in one request, so hops cannot page past them; narrow the "
                                "query with --grep, or a selector with --query")
            rest = [e for e in tied if key(e) not in seen_at_start]
            found += rest[:limit - len(found)]
            if len(found) >= limit:
                return found, True
            start, seen_at_start = start + 1, set()
            if start >= end:
                return found, False
            continue
        start = found[-1][0]
        seen_at_start = {key(e) for e in found if e[0] == start}
    return found, True


def key(entry: tuple[int, dict, str]) -> tuple:
    ns, labels, line = entry
    return ns, tuple(sorted(labels.items())), line
