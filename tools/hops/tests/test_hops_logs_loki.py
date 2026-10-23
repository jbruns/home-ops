"""hops logs --loki: history from Loki's query_range, asked from an ad-hoc container on the Host."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

RECORDED = Path(__file__).parent / "fixtures" / "loki" / "query_range.json"
PYTHON = "python:3.14.8-alpine3.24@sha256:4444"


@pytest.fixture(autouse=True)
def transport_image(repo):
    """The fixture monitoring Stack gains the Service whose image carries the request, and doco-cd
    its second Service, as in the repo."""
    compose = repo / "tn" / "monitoring" / "compose.yaml"
    compose.write_text(compose.read_text() + f"  dependencies-render:\n    image: {PYTHON}\n"
                       "    container_name: dependencies-render\n")
    doco = repo / "tn" / "doco-cd" / "compose.yaml"
    doco.write_text(doco.read_text() + "  apprise:\n    image: caronc/apprise:2.0.1\n")


def request(call) -> dict:
    """The query_range parameters of a call, from the URL the container is given."""
    url = urlsplit(next(a for a in call.docker[-1] if a.startswith("http://")))
    assert (url.netloc, url.path) == ("loki:3100", "/loki/api/v1/query_range")
    return {k: v[0] for k, v in parse_qs(url.query).items()}


def streams(*entries) -> str:
    """A query_range answer: entries are (ns, line, labels)."""
    by_labels = {}
    for ns, line, labels in entries:
        by_labels.setdefault(json.dumps(labels, sort_keys=True), []).append([str(ns), line])
    result = [{"stream": json.loads(k), "values": v} for k, v in by_labels.items()]
    return json.dumps({"status": "success", "data": {"resultType": "streams", "result": result}})


class FakeLoki:
    """Answers query_range from a fixed set of entries as Loki does: forward, start inclusive,
    end exclusive, at most `limit` entries across streams."""

    def __init__(self, entries):
        self.entries = sorted(entries, key=lambda e: e[0])
        self.requests = []

    def __call__(self, call) -> str:
        q = request(call)
        self.requests.append(q)
        assert q["direction"] == "forward"
        hits = [e for e in self.entries if int(q["start"]) <= e[0] < int(q["end"])]
        return streams(*hits[:int(q["limit"])])


SONARR = {"container": "sonarr", "logstream": "stdout", "service": "sonarr", "stack": "sonarr"}


def test_a_named_container_is_selected_by_container_so_history_survives_recreation(hops, fake_host):
    loki = FakeLoki([(1_000, "old line", SONARR)])
    fake_host.respond("run", stdout=loki)
    outcome = hops("logs", "sonarr/sonarr", "--loki", "--since", "2026-10-20T00:00:00Z",
                   "--until", "2026-10-21T00:00:00Z", "--json")
    assert outcome.code == 0, outcome.err
    q = loki.requests[0]
    assert q["query"] == '{container="sonarr", logstream=~"stdout|stderr"}'
    assert (q["start"], q["end"]) == ("1792454400000000000", "1792540800000000000")
    report = json.loads(outcome.out)
    assert report["query"] == q["query"]
    assert (report["stack"], report["service"], report["container"]) == ("sonarr", "sonarr", "sonarr")


def test_a_bare_stack_selects_every_service_in_it(hops, fake_host):
    loki = FakeLoki([])
    fake_host.respond("run", stdout=loki)
    assert hops("logs", "nextcloud", "--loki", "--json").code == 0
    assert loki.requests[0]["query"] == (
        '{container=~"nextcloud-exporter|mariadb|nextcloud|valkey", logstream=~"stdout|stderr"}')


def test_a_service_without_container_name_matches_every_container_compose_gave_it(hops, fake_host):
    loki = FakeLoki([])
    fake_host.respond("run", stdout=loki)
    hops("logs", "doco-cd/doco-cd", "--loki", "--grep", 'deploy"ed')
    assert loki.requests[0]["query"] == (
        '{container=~"doco-cd-doco-cd-[0-9]+", logstream=~"stdout|stderr"} |~ "deploy\\"ed"')


def test_a_name_that_is_both_stack_and_service_means_the_stack(hops, fake_host):
    loki = FakeLoki([(1, "sent", {"container": "doco-cd-apprise-1", "service": "apprise", "logstream": "stdout"}),
                     (2, "deployed", {"container": "doco-cd-doco-cd-6", "service": "doco-cd", "logstream": "stderr"})])
    fake_host.respond("run", stdout=loki)
    outcome = hops("logs", "doco-cd", "--loki", "--since", "1970-01-01T00:00:00Z")
    assert loki.requests[0]["query"] == (
        '{container=~"doco-cd-apprise-[0-9]+|doco-cd-doco-cd-[0-9]+", logstream=~"stdout|stderr"}')
    assert outcome.out.splitlines() == ["1970-01-01T00:00:00.000000001Z apprise | sent",
                                        "1970-01-01T00:00:00.000000002Z doco-cd | deployed"]


def test_query_passes_logql_through(hops, fake_host):
    loki = FakeLoki([])
    fake_host.respond("run", stdout=loki)
    outcome = hops("logs", "--loki", "--query", '{logstream="access"} |= "500"', "--json")
    assert outcome.code == 0, outcome.err
    assert loki.requests[0]["query"] == '{logstream="access"} |= "500"'
    report = json.loads(outcome.out)
    assert (report["stack"], report["service"], report["container"]) == (None, None, None)


def test_pages_until_the_limit_without_repeating_lines_that_share_a_timestamp(hops, fake_host, monkeypatch):
    from hops import loki as loki_module

    monkeypatch.setattr(loki_module, "PAGE", 3)
    other = {**SONARR, "logstream": "stderr"}
    loki = FakeLoki([(1, "a", SONARR), (2, "b", SONARR), (3, "c", SONARR), (3, "c2", other),
                     (4, "d", SONARR), (5, "e", SONARR), (6, "f", SONARR), (7, "g", SONARR)])
    fake_host.respond("run", stdout=loki)
    outcome = hops("logs", "sonarr/sonarr", "--loki", "--since", "1970-01-01T00:00:00Z", "--limit", "6", "--json")
    assert outcome.code == 0, outcome.err
    lines = json.loads(outcome.out)["lines"]
    assert [line["text"] for line in lines] == ["a", "b", "c", "c2", "d", "e"]
    assert json.loads(outcome.out)["truncated"] is True
    assert len(loki.requests) > 1
    assert all(int(q["limit"]) <= 3 for q in loki.requests)


def test_stops_when_loki_has_no_more(hops, fake_host):
    loki = FakeLoki([(1, "a", SONARR), (2, "b", SONARR)])
    fake_host.respond("run", stdout=loki)
    report = json.loads(hops("logs", "sonarr/sonarr", "--loki", "--since", "1970-01-01T00:00:00Z", "--json").out)
    assert [line["text"] for line in report["lines"]] == ["a", "b"]
    assert report["truncated"] is False
    assert len(loki.requests) == 1


def test_a_recorded_response_prints_like_hops_logs_with_loki_labels(hops, fake_host):
    fake_host.respond("run", stdout=RECORDED.read_text())
    report = json.loads(hops("logs", "doco-cd/doco-cd", "--loki", "--limit", "3", "--json").out)
    assert (report["stack"], report["service"], report["container"]) == ("doco-cd", "doco-cd", None)
    first = report["lines"][0]
    assert first["time"] == "2026-10-18T00:01:20.526870147Z"
    assert first["stream"] == "stderr"
    assert first["text"].startswith('{"time":"2026-10-17T17:01:20.526539876-07:00","level":"info",'
                                    '"msg":"polling repository"')
    assert first["labels"]["container"] == "doco-cd-doco-cd-6"
    assert len(report["lines"]) == 3

    text = hops("logs", "doco-cd/doco-cd", "--loki", "--limit", "3").out.splitlines()
    assert len(text) == 3
    assert text[0].startswith('2026-10-18T00:01:20.526870147Z {"time":')


def test_the_request_goes_from_a_throwaway_container_that_loki_logs_ignore(hops, fake_host):
    fake_host.respond("run", stdout=streams())
    hops("logs", "traefik", "--loki")
    args = fake_host.calls[0].docker[-1]
    assert args[:2] == ["run", "--rm"]
    assert "startup-order=skip" in args
    assert args[args.index("--network") + 1] == "monitoring_default"
    assert args[args.index("--log-driver") + 1] == "none"
    assert args[args.index("--pull") + 1] == "never"
    assert PYTHON in args


def test_a_loki_error_is_reported_with_its_message(hops, fake_host):
    fake_host.respond("run", returncode=3, stderr="HTTP 400: parse error at line 1, col 2: syntax error\n")
    outcome = hops("logs", "--loki", "--query", "{oops")
    assert outcome.code == 1
    assert "parse error at line 1" in outcome.err


@pytest.mark.parametrize("argv", [
    ["logs", "traefik", "--loki", "--tail", "5"],
    ["logs", "traefik", "--loki", "-f"],
    ["logs", "traefik", "--until", "1h"],
    ["logs", "traefik", "--grep", "x"],
    ["logs", "--loki"],
    ["logs", "traefik", "--loki", "--query", "{a=\"b\"}"],
    ["logs", "traefik", "--loki", "--since", "yesterday"],
])
def test_bad_combinations_fail_before_the_host(hops, fake_host, argv):
    assert hops(*argv).code == 2
    assert fake_host.calls == []


def test_a_full_page_of_lines_at_one_timestamp_does_not_stop_paging(hops, fake_host, monkeypatch):
    from hops import loki as loki_module

    monkeypatch.setattr(loki_module, "PAGE", 3)
    tied = [(1, f"tied {i}", SONARR) for i in range(3)]
    loki = FakeLoki([*tied, (2, "after", SONARR)])
    fake_host.respond("run", stdout=loki)
    outcome = hops("logs", "sonarr/sonarr", "--loki", "--since", "1970-01-01T00:00:00Z", "--limit", "6", "--json")
    assert outcome.code == 0, outcome.err
    report = json.loads(outcome.out)
    assert sorted(line["text"] for line in report["lines"]) == ["after", "tied 0", "tied 1", "tied 2"]
    assert report["lines"][-1]["text"] == "after"
    assert report["truncated"] is False


def test_more_tied_lines_than_loki_returns_at_once_fail_clearly(hops, fake_host, monkeypatch):
    from hops import loki as loki_module

    monkeypatch.setattr(loki_module, "PAGE", 3)
    monkeypatch.setattr(loki_module, "MAX_ENTRIES", 3)
    loki = FakeLoki([*((1, f"tied {i}", SONARR) for i in range(3)), (2, "after", SONARR)])
    fake_host.respond("run", stdout=loki)
    outcome = hops("logs", "sonarr/sonarr", "--loki", "--since", "1970-01-01T00:00:00Z", "--limit", "6")
    assert outcome.code == 1
    assert "share the timestamp 1970-01-01T00:00:00.000000001Z" in outcome.err
    assert "--grep" in outcome.err


def test_absolute_times_keep_nanoseconds(hops, fake_host):
    """A time copied from hops' own output bounds the range to the nanosecond; --until is exclusive."""
    first = 1792281680526870147  # 2026-10-18T00:01:20.526870147Z, the recorded fixture's first line
    loki = FakeLoki([(first - 1, "before", SONARR), (first, "at", SONARR), (first + 1, "after", SONARR)])
    fake_host.respond("run", stdout=loki)
    outcome = hops("logs", "sonarr/sonarr", "--loki", "--since", "2026-10-18T00:01:20.526870147Z",
                   "--until", "2026-10-18T00:01:20.526870148Z", "--json")
    assert (loki.requests[0]["start"], loki.requests[0]["end"]) == (str(first), str(first + 1))
    assert [line["text"] for line in json.loads(outcome.out)["lines"]] == ["at"]


def test_absolute_times_with_an_offset_keep_nanoseconds(hops, fake_host):
    loki = FakeLoki([])
    fake_host.respond("run", stdout=loki)
    hops("logs", "sonarr/sonarr", "--loki", "--since", "2026-10-17T17:01:20.526539876-07:00",
         "--until", "2026-10-18T00:01:21Z")
    assert loki.requests[0]["start"] == "1792281680526539876"


def test_grep_keeps_characters_outside_the_basic_plane(hops, fake_host):
    loki = FakeLoki([])
    fake_host.respond("run", stdout=loki)
    hops("logs", "sonarr/sonarr", "--loki", "--grep", "ok 😀")
    assert loki.requests[0]["query"].endswith('|~ "ok 😀"')
