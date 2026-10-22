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
