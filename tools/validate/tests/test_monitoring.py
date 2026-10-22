import json
import urllib.request
import subprocess
from urllib.parse import parse_qs, urlparse

from conftest import commit, git, write


def monitoring_errors(validate) -> str:
    validate.failures.clear()
    validate.check_monitoring()
    return "\n".join(validate.failures)


def test_monitoring_reports_the_dashboard_panel_and_invalid_query(validate, repo, runtime):
    write(repo, {"tn/monitoring/grafana/dashboards/overview.json": json.dumps({
        "uid": "overview", "title": "Tier overview",
        "panels": [{"title": "Uptime", "datasource": {"type": "prometheus", "uid": "prometheus"},
                    "targets": [{"refId": "A", "expr": "broken_query("}]}],
    })})
    errors = monitoring_errors(validate)
    assert "Tier overview" in errors and "Uptime" in errors and "broken_query(" in errors
    assert "parse error" in errors


def test_monitoring_retries_startup_connection_resets_and_removes_its_containers(validate, runtime, monkeypatch):
    urlopen = urllib.request.urlopen
    attempts = []

    def resetting(request, **kwargs):
        attempts.append(request.full_url)
        if len(attempts) == 1:
            raise ConnectionResetError("starting")
        return urlopen(request, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", resetting)
    assert monitoring_errors(validate) == ""
    assert len(attempts) > 1
    assert len([c for c in runtime.commands if c[1] == "rm"]) == 2


def test_monitoring_rejects_alerts_without_firing_and_nonfiring_cases(validate, repo, runtime):
    write(repo, {"tn/monitoring/prometheus/alerting-rules.yml": """
        groups:
          - name: example
            rules:
              - alert: Example
                expr: up == 0
                for: 1m
    """})
    assert "Example: add firing and nonfiring" in monitoring_errors(validate)


def test_monitoring_requires_alert_cases_to_pin_for_and_routing_labels(validate, repo, runtime):
    write(repo, {
        "tn/monitoring/prometheus/alerting-rules.yml": """
            groups:
              - name: example
                rules:
                  - {alert: Example, expr: 'up == 0', for: 5m, labels: {tier: warning, stack: app}}
        """,
        "tools/validate/rules/example.test.yml": """
            rule_files: [/etc/prometheus/alerting-rules.yml]
            tests:
              - input_series: [{series: up, values: '0x10'}]
                promql_expr_test:
                  - {expr: 'ALERTS{alertname="Example",alertstate="firing"}', eval_time: 0m, exp_samples: []}
                  - {expr: 'ALERTS{alertname="Example",alertstate="firing"}', eval_time: 6m,
                     exp_samples: [{labels: 'ALERTS{alertname="Example",tier="warning"}', value: 1}]}
        """,
    })
    errors = monitoring_errors(validate)
    assert "Example: assert stack and tier in every firing expectation" in errors
    assert "Example: pin the for: 5m boundary" in errors
    write(repo, {"tools/validate/rules/example.test.yml": (repo / "tools/validate/rules/example.test.yml")
                 .read_text().replace("eval_time: 0m", "eval_time: 1m")
                 .replace('tier="warning"', 'tier="warning",stack="app"')})
    assert monitoring_errors(validate) == ""


def test_monitoring_fails_on_provisioning_errors_even_when_grafana_is_healthy(validate, runtime, monkeypatch):
    run = subprocess.run

    def errors(cmd, **kwargs):
        result = run(cmd, **kwargs)
        if cmd[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(cmd, 0, "", "logger=provisioning.datasources level=error msg=failed\n")
        return result

    monkeypatch.setattr(subprocess, "run", errors)
    assert "Grafana provisioning error" in monitoring_errors(validate)
    assert any(cmd[1] == "rm" for cmd in runtime.commands)


def test_monitoring_requires_changed_rules_to_update_their_behavioral_case(validate, repo, runtime):
    rules = "groups: [{name: example, rules: [{alert: Example, expr: 'up == 0', labels: {tier: warning, stack: app}}]}]\n"
    suite = """
        rule_files: [/etc/prometheus/alerting-rules.yml]
        tests:
          - name: Example fires only for a down target
            input_series: [{series: up, values: '0 1'}]
            promql_expr_test:
              - {expr: 'ALERTS{alertname="Example",alertstate="firing"}', eval_time: 0m,
                 exp_samples: [{labels: 'ALERTS{alertname="Example",alertstate="firing",tier="warning",stack="app"}', value: 1}]}
              - {expr: 'ALERTS{alertname="Example",alertstate="firing"}', eval_time: 1m, exp_samples: []}
    """
    write(repo, {"tn/monitoring/prometheus/alerting-rules.yml": rules,
                 "tools/validate/rules/example.test.yml": suite})
    commit(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    write(repo, {"tn/monitoring/prometheus/alerting-rules.yml": rules.replace("up == 0", "up < 1")})
    assert "Example: rule changed" in monitoring_errors(validate)
    write(repo, {"tools/validate/rules/example.test.yml": suite.replace(
        "Example fires only for a down target", "Only a cosmetic test rename")})
    assert "Example: rule changed" in monitoring_errors(validate)
    write(repo, {"tools/validate/rules/example.test.yml": suite.replace("values: '0 1'", "values: '0 2'")})
    assert monitoring_errors(validate) == ""
    assert any(r.args[:3] == ["promtool", "test", "rules"] for r in runtime.runs)


def test_monitoring_rejects_datasource_placeholders_missing_from_the_encrypted_environment(validate, repo, runtime):
    write(repo, {"tn/monitoring/grafana/datasources.sops.env": "GRAFANA_DS_PROMETHEUS_URL=ENC[x]\nsops_version=3\n"})
    assert "environment is missing GRAFANA_DS_LOKI_URL" in monitoring_errors(validate)


def test_monitoring_checks_nested_logql_variables_annotations_and_grafana_macros(validate, repo, runtime):
    write(repo, {"tn/monitoring/grafana/dashboards/overview.json": json.dumps({
        "uid": "overview", "title": "Service",
        "templating": {"list": [
            {"name": "stack", "type": "query", "datasource": {"uid": "prometheus"},
             "query": {"query": "label_values(up, stack)"}},
            {"name": "ds", "type": "datasource", "query": "prometheus", "regex": "/^Prometheus$/"},
        ]},
        "annotations": {"list": [{"name": "Deploys", "datasource": {"uid": "prometheus"},
                                  "expr": "broken_query("}]},
        "panels": [{"type": "row", "title": "Details", "panels": [
            {"title": "Logs", "datasource": {"uid": "loki"},
             "targets": [{"refId": "A", "expr": 'broken_query({stack="$stack"}[$__auto])', "hide": True}]},
            {"title": "CPU", "datasource": {"uid": "${ds}"},
             "targets": [{"refId": "A", "expr": 'label_replace(rate(cpu{stack="${stack:regex}"}[$__rate_interval]), "a", "$1", "b", "(.+)")'}]},
        ]}],
    })})
    errors = monitoring_errors(validate)
    assert "annotation Deploys" in errors and "Details / Logs / A: loki query" in errors
    assert "CPU" not in errors
    queries = [parse_qs(urlparse(url).query)["query"][0] for url in runtime.requests if "query=" in url]
    assert "up" in queries
    assert 'label_replace(rate(cpu{stack="ci_value"}[5m]), "a", "$1", "b", "(.+)")' in queries


def test_monitoring_rejects_unknown_datasource_uids_and_undeclared_variables(validate, repo, runtime):
    write(repo, {"tn/monitoring/grafana/dashboards/overview.json": json.dumps({
        "uid": "overview", "title": "Service",
        "panels": [
            {"title": "Typo", "datasource": {"uid": "promethues"}, "targets": [{"expr": "up"}]},
            {"title": "Missing variable", "datasource": {"uid": "prometheus"},
             "targets": [{"expr": 'up{stack="$missing"}'}]},
        ],
    })})
    errors = monitoring_errors(validate)
    assert "Typo: unknown datasource uid 'promethues'" in errors
    assert "Missing variable" in errors and "undeclared Grafana variable $missing" in errors


def test_monitoring_requires_templated_routing_labels_to_be_pinned_non_empty(validate, repo, runtime):
    def rule(expr, stack='"{{ $labels.project }}"'):
        return f"""
            groups:
              - name: example
                rules:
                  - alert: Example
                    expr: '{expr}'
                    labels: {{tier: warning, stack: {stack}}}
        """
    write(repo, {"tn/monitoring/prometheus/alerting-rules.yml": rule("x == 0")})
    assert "Example: stack can render empty or literally; pin project" in monitoring_errors(validate)
    for expr in ['x{project!=""} == 0', 'x{project=~".+"} == 0', 'x{project="app"} == 0',
                 'label_replace(x, "project", "ops", "project", "") == 0']:
        write(repo, {"tn/monitoring/prometheus/alerting-rules.yml": rule(expr.replace("'", "''"))})
        assert "can render empty" not in monitoring_errors(validate), expr
    write(repo, {"tn/monitoring/prometheus/alerting-rules.yml": rule('x{project=~".*"} == 0')})
    assert "Example: stack can render empty" in monitoring_errors(validate)
    write(repo, {"tn/monitoring/prometheus/alerting-rules.yml": rule("x == 0", stack='"{{ $labels.a }}-{{ $labels.b }}"')})
    assert "Example: stack can render empty" in monitoring_errors(validate)
