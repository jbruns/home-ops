"""hops deploys: doco-cd's run history, from responses recorded on the Host (tests/fixtures/doco).

Successful runs and the empty list are recorded as returned. doco-cd had no failed or skipped run in
memory when they were recorded: the failed run is rebuilt from doco-cd's log of that job, and the
skipped ones use the messages doco-cd 0.128 writes for a paused Stack and an exhausted retry budget.
"""
import json
from pathlib import Path

import pytest

from hops import doco
from hops_fakes import container

FIXTURES = Path(__file__).parent / "fixtures" / "doco"
KEY = "fake-doco-cd-key"
SUCCEEDED = "01a1693c-5d48-709e-92f3-d5fbed5edfc0"
PAUSED = "01a16935-0b6e-7c1e-9a51-2f6b0d1c8e40"
BUDGET = "01a160c2-5f1a-7b33-8d0e-4c2a9e7b6f11"
FAILED = "01a160a4-3901-7c7d-b694-0570e23af5f6"


def recorded(name: str, status: str = "200 OK", only: str | None = None) -> str:
    """A fixture as the transport returns it: HTTP/1.0 headers, CRLFs already read as LFs.
    `only` keeps the runs with that status, as doco-cd's status filter does."""
    body = (FIXTURES / name).read_text()
    if only:
        data = json.loads(body)
        body = json.dumps({**data, "content": [r for r in data["content"] if r["status"] == only]})
    return f"HTTP/1.0 {status}\nContent-Type: application/json\nContent-Length: {len(body)}\n\n{body}"


@pytest.fixture
def doco_cd(fake_host, monkeypatch):
    """prometheus can carry requests to doco-cd and `sops -d` returns KEY; doco_cd(name) sets the answer."""
    monkeypatch.setattr(doco, "decrypt", lambda path: KEY + "\n")
    fake_host.respond("network", "inspect", "metrics", stdout="prometheus \n")
    fake_host.respond("inspect", stdout=json.dumps([container("prometheus", project="monitoring", service="prometheus")]))
    fake_host.respond("exec", "prometheus", "sh")

    def answer(name: str, status: str = "200 OK", only: str | None = None):
        fake_host.respond("exec", "-i", "prometheus", stdout=recorded(name, status, only))

    return answer


def sent(fake_host) -> list[str]:
    """The request line of each request sent to doco-cd."""
    return [c.input.split("\r\n", 1)[0] for c in fake_host.calls if c.docker[-1][:2] == ["exec", "-i"]]


def test_lists_recent_runs_newest_first_with_their_messages(hops, doco_cd, fake_host):
    doco_cd("runs.json")
    outcome = hops("deploys")
    assert outcome.code == 0, outcome.err
    assert sent(fake_host) == ["GET /v1/api/runs?limit=200 HTTP/1.0"]
    assert all(KEY not in arg for call in fake_host.calls for arg in call.argv)
    lines = outcome.out.splitlines()
    assert lines[0].split() == ["CREATED", "RUN", "TRIGGER", "STATUS", "STACKS", "MESSAGE"]
    assert [line.split()[1] for line in lines[1:]] == [SUCCEEDED, PAUSED, BUDGET, FAILED]
    assert "2026-10-23T07:48:46-07:00" in lines[1] and "22 Stacks" in lines[1]
    assert "skipped" in lines[3] and "budget_exhausted" in lines[3]
    assert "navidrome, ownfoil" in lines[4] and "service ownfoil has a restarting container" in lines[4]


def test_a_stack_shows_only_the_runs_that_touched_it(hops, doco_cd, fake_host):
    doco_cd("runs.json")
    outcome = hops("deploys", "plex")
    assert outcome.code == 0, outcome.err
    assert sent(fake_host) == ["GET /v1/api/runs?limit=200 HTTP/1.0"]
    assert [line.split()[1] for line in outcome.out.splitlines()[1:]] == [SUCCEEDED, PAUSED]
    assert "deployment paused: `plex`" in outcome.out


def test_status_is_asked_of_doco_cd_and_limit_applies_after_the_stack_filter(hops, doco_cd, fake_host):
    doco_cd("runs.json", only="skipped")
    outcome = hops("deploys", "ownfoil", "--status", "skipped", "--limit", "1", "--json")
    assert outcome.code == 0, outcome.err
    assert sent(fake_host) == ["GET /v1/api/runs?limit=200&status=skipped HTTP/1.0"]
    assert [r["job_id"] for r in json.loads(outcome.out)["runs"]] == [BUDGET]


def test_json_lists_each_run_with_its_stacks_and_message(hops, doco_cd):
    doco_cd("runs.json")
    runs = json.loads(hops("deploys", "--json").out)["runs"]
    assert runs[3] == {
        "job_id": FAILED, "trigger": "poll", "status": "failed",
        "repository": "github.com/jbruns/home-ops", "revision": "main",
        "stacks": ["navidrome", "ownfoil"],
        "message": "deployment failed: failed to deploy stack ownfoil: failed to deploy stack: "
                   "service ownfoil has a restarting container",
        "created_at": "2026-10-21T15:45:37.666093522-07:00",
        "started_at": "2026-10-21T15:45:37.666151807-07:00",
        "finished_at": "2026-10-21T15:45:47.880302816-07:00",
    }


def test_an_empty_history_says_doco_cd_keeps_it_in_memory_only(hops, doco_cd):
    doco_cd("runs_empty.json")
    outcome = hops("deploys", "--status", "failed")
    assert outcome.code == 0, outcome.err
    assert "No runs with status failed" in outcome.out
    assert "in memory only" in outcome.out and "hops logs --loki doco-cd" in outcome.out


def test_a_run_id_shows_that_run_in_detail(hops, doco_cd, fake_host):
    doco_cd("run_failed.json")
    outcome = hops("deploys", FAILED)
    assert outcome.code == 0, outcome.err
    assert sent(fake_host) == [f"GET /v1/api/run/{FAILED} HTTP/1.0"]
    assert "Status:   failed" in outcome.out and "Stacks:   navidrome, ownfoil" in outcome.out
    assert "Finished: 2026-10-21T15:45:47-07:00" in outcome.out
    assert "  deployment failed: failed to deploy stack ownfoil" in outcome.out


def test_an_unknown_run_points_at_the_history_limits(hops, doco_cd):
    doco_cd("run_not_found.json", "404 Not Found")
    outcome = hops("deploys", "00000000-0000-0000-0000-000000000000")
    assert outcome.code == 1
    assert "run not found" in outcome.err and "in memory only" in outcome.err


def test_a_stack_the_checkout_does_not_declare_is_still_looked_up(hops, doco_cd):
    """doco-cd's history can name a Stack since removed from the repo."""
    doco_cd("runs.json")
    outcome = hops("deploys", "retired")
    assert outcome.code == 0, outcome.err
    assert "No runs for retired. (retired is not a Stack in this checkout.)" in outcome.out


@pytest.mark.parametrize("argv", [["--limit", "0"], [FAILED, "--status", "failed"], [FAILED, "--limit", "5"]])
def test_bad_options_fail_before_touching_the_host(hops, fake_host, argv):
    assert hops("deploys", *argv).code == 2
    assert fake_host.calls == []
