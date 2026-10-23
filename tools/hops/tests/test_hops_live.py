"""Read-only checks against the real Host. Skipped unless selected: python3 -m pytest tools/hops -m live

Needs TRUENAS_HOST (repo-root .env or environment) and the agent SSH key. Never add a command
here that changes anything on the Host.
"""
import json

import pytest

from hops import cli

pytestmark = pytest.mark.live


def hops_json(capsys, *argv):
    code = cli.main([*argv, "--json"], ctx=cli.Context())
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out)


def test_status_sees_the_declared_services(capsys):
    report = hops_json(capsys, "status")
    states = {f"{r['stack']}/{r['service']}": r["state"] for r in report["services"]}
    assert states["monitoring/prometheus"] == "running"
    assert "undeclared" in report


def test_inspect_prometheus(capsys):
    report = hops_json(capsys, "inspect", "monitoring/prometheus")
    assert report["state"] == "running"
    assert report["memory"]["limit_bytes"] > 0


def test_logs_tail(capsys):
    report = hops_json(capsys, "logs", "monitoring/prometheus", "--tail", "2")
    assert report["container"] == "prometheus"
    assert len(report["lines"]) <= 4  # up to 2 per stream


def test_alerts_include_the_watchdog(capsys):
    report = hops_json(capsys, "alerts")
    assert "Watchdog" in {a["labels"].get("alertname") for a in report["alerts"]}


def test_logs_loki_reads_history_oldest_first(capsys):
    report = hops_json(capsys, "logs", "doco-cd/doco-cd", "--loki", "--since", "1d", "--limit", "5")
    times = [line["time"] for line in report["lines"]]
    assert times == sorted(times)
    assert all(line["labels"]["service"] == "doco-cd" for line in report["lines"])


def test_deploys_lists_doco_cds_runs(capsys):
    runs = hops_json(capsys, "deploys", "--limit", "3")["runs"]
    assert len(runs) <= 3
    if runs:  # empty right after doco-cd restarts
        assert hops_json(capsys, "deploys", runs[0]["job_id"])["job_id"] == runs[0]["job_id"]
