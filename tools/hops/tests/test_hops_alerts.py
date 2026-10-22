"""hops alerts: firing alerts and active silences, from amtool inside the alertmanager container."""
import json
import os
import subprocess

import yaml

ALERTS = [{
    "annotations": {"summary": "nextcloud is down"},
    "labels": {"alertname": "ContainerDown", "stack": "nextcloud", "tier": "critical"},
    "startsAt": "2026-10-21T20:58:05.738Z", "endsAt": "2026-10-22T04:31:05.738Z",
    "fingerprint": "45c9", "receivers": [{"name": "pushover"}], "updatedAt": "2026-10-22T04:27:05.740Z",
    "status": {"inhibitedBy": [], "mutedBy": [], "silencedBy": ["s1"], "state": "suppressed"},
}]
SILENCES = [{
    "id": "s1", "createdBy": "jbruns", "comment": "#543 window",
    "startsAt": "2026-10-21T20:00:00Z", "endsAt": "2026-10-22T02:00:00Z", "updatedAt": "2026-10-21T20:00:00Z",
    "matchers": [{"name": "stack", "value": "plex|emby", "isRegex": True, "isEqual": True},
                 {"name": "alertname", "value": "Watchdog", "isRegex": False, "isEqual": False}],
    "status": {"state": "active"},
}]
PASSWORD = ["exec", "prometheus", "cat", "/run/secrets/prometheus_password"]


def respond(fake_host):
    fake_host.respond("exec", "-i", "alertmanager", suffix=("alert", "query", "--active", "--silenced",
                      "--inhibited", "-o", "json"), stdout=json.dumps(ALERTS))
    fake_host.respond("exec", "-i", "alertmanager", suffix=("silence", "query", "-o", "json"),
                      stdout=json.dumps(SILENCES))


def amtool_args(call):
    """The arguments the remote sh passes to amtool ("$@" after $0=amtool)."""
    alertmanager = call.docker[1]
    return alertmanager[alertmanager.index("amtool") + 1:]


def test_alerts_query_amtool_with_the_password_piped_on_the_host(hops, fake_host):
    respond(fake_host)
    outcome = hops("alerts", "--json")
    assert outcome.code == 0
    first, second = fake_host.calls
    assert first.docker[0] == PASSWORD and second.docker[0] == PASSWORD
    assert first.docker[1][:4] == ["exec", "-i", "alertmanager", "sh"]
    assert amtool_args(first) == ["alert", "query", "--active", "--silenced", "--inhibited", "-o", "json"]
    assert amtool_args(second) == ["silence", "query", "-o", "json"]
    assert first.input is None


def test_alerts_json_lists_alerts_and_silences(hops, fake_host):
    respond(fake_host)
    assert json.loads(hops("alerts", "--json").out) == {
        "alerts": [{
            "labels": {"alertname": "ContainerDown", "stack": "nextcloud", "tier": "critical"},
            "annotations": {"summary": "nextcloud is down"},
            "state": "suppressed", "starts_at": "2026-10-21T20:58:05.738Z",
            "silenced_by": ["s1"], "inhibited_by": [],
        }],
        "silences": [{
            "id": "s1", "state": "active", "matchers": ['stack=~"plex|emby"', 'alertname!="Watchdog"'],
            "starts_at": "2026-10-21T20:00:00Z", "ends_at": "2026-10-22T02:00:00Z",
            "created_by": "jbruns", "comment": "#543 window",
        }],
    }


def test_alerts_table(hops, fake_host):
    respond(fake_host)
    out = hops("alerts").out
    assert "ContainerDown" in out and "nextcloud is down" in out
    assert 'stack=~"plex|emby"' in out and "#543 window" in out


def test_amtool_script_reads_the_password_from_stdin_as_yaml(hops, fake_host, tmp_path):
    """Run the remote sh script locally, with an amtool stub that prints the config it was given."""
    respond(fake_host)
    hops("alerts", "--json")
    script = fake_host.calls[0].docker[1][5]
    stub = tmp_path / "amtool"
    stub.write_text('#!/bin/sh\nfor a; do case "$a" in --http.config.file=*) cat "${a#*=}";; esac; done\n')
    stub.chmod(0o755)
    password = "it's: #secret"
    done = subprocess.run(["sh", "-c", script, "amtool", "alert", "query"], input=password + "\n",
                          capture_output=True, text=True, env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})
    assert yaml.safe_load(done.stdout) == {"basic_auth": {"username": "prometheus", "password": password}}
    assert password not in script
