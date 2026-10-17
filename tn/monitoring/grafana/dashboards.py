#!/usr/bin/env python3
"""Generate the Grafana dashboards provisioned from the repo (stdlib only).

    python3 tn/monitoring/grafana/dashboards.py           # write dashboards/*.json
    python3 tn/monitoring/grafana/dashboards.py --check   # fail if they are stale (validate.py)

Edit this file, never the JSON. Vocabulary follows GLOSSARY.md: Service, Stack, Tier, Probe,
Uptime, Availability, Dependency. Datasources are referenced by the uids set in
provisioning/datasources.sops.yml.
"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}

TIERS = ("critical", "warning", "info")
UP_MAPPING = [{"type": "value", "options": {
    "0": {"text": "DOWN", "color": "red", "index": 0},
    "1": {"text": "UP", "color": "green", "index": 1},
}}]
AVAILABILITY_STEPS = [
    {"color": "red", "value": None},
    {"color": "orange", "value": 0.99},
    {"color": "green", "value": 0.999},
]

# Traefik's service label is the container name plus its provider (`sonarr@docker`);
# recording-rules.yml makes the same assumption.
TRAEFIK_CONTAINER = 'label_replace({expr}, "container", "$1", "service", "(.+)@.+")'


# ---------------------------------------------------------------------------- building blocks

def target(expr, legend="", ref="A", instant=False, table=False, ds=PROM):
    t = {"refId": ref, "datasource": ds, "expr": expr, "legendFormat": legend}
    if instant:
        t |= {"instant": True, "range": False}
    if table:
        t["format"] = "table"
    return t


def panel(kind, title, targets, w, h, unit=None, desc=None, ds=PROM, **extra):
    defaults = {"custom": {}}
    if unit:
        defaults["unit"] = unit
    p = {
        "type": kind,
        "title": title,
        "datasource": ds,
        "targets": targets,
        "gridPos": {"w": w, "h": h},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {},
    }
    if desc:
        p["description"] = desc
    for k, v in extra.items():
        if k in ("defaults", "custom"):
            (defaults if k == "defaults" else defaults["custom"]).update(v)
        elif k == "overrides":
            p["fieldConfig"]["overrides"] = v
        else:
            p[k] = v
    return p


def stat(title, expr, w=4, h=4, unit=None, mappings=None, steps=None, desc=None, decimals=None):
    d = {}
    if mappings:
        d["mappings"] = mappings
    if steps:
        d["thresholds"] = {"mode": "absolute", "steps": steps}
    if decimals is not None:
        d["decimals"] = decimals
    return panel("stat", title, [target(expr, instant=True)], w, h, unit, desc, defaults=d,
                 options={"colorMode": "background", "graphMode": "none", "textMode": "value",
                          "reduceOptions": {"calcs": ["lastNotNull"]}})


def timeseries(title, targets, w=12, h=8, unit=None, desc=None, stack=False, **extra):
    custom = {"fillOpacity": 10, "showPoints": "never", "lineWidth": 1}
    if stack:
        custom |= {"stacking": {"mode": "normal"}, "fillOpacity": 40}
    return panel("timeseries", title, targets, w, h, unit, desc, custom=custom,
                 options={"legend": {"displayMode": "list", "placement": "bottom"},
                          "tooltip": {"mode": "multi", "sort": "desc"}}, **extra)


def state_timeline(title, targets, w=12, h=8, mappings=UP_MAPPING, desc=None):
    return panel("state-timeline", title, targets, w, h, desc=desc,
                 defaults={"mappings": mappings, "color": {"mode": "thresholds"},
                           "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None},
                                                                         {"color": "green", "value": 1}]}},
                 custom={"fillOpacity": 80, "lineWidth": 0},
                 options={"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                          "legend": {"showLegend": False}})


def table(title, targets, columns, w=12, h=8, join="container", overrides=(), desc=None, sort=None):
    """Instant queries joined on `join`. `columns` maps "Value #<ref>" to a display name."""
    hide = {"Time": True} | {f"Time {i}": True for i in range(1, 10)}
    renames = dict(columns)
    order = {join: 0} | {name: i + 1 for i, name in enumerate(columns)}
    transforms = [
        {"id": "joinByField", "options": {"byField": join, "mode": "outer"}},
        {"id": "organize", "options": {"excludeByName": hide, "renameByName": renames, "indexByName": order}},
    ]
    opts = {"showHeader": True, "cellHeight": "sm"}
    if sort:
        opts["sortBy"] = [{"displayName": sort, "desc": False}]
    return panel("table", title, targets, w, h, desc=desc, transformations=transforms,
                 options=opts, custom={"align": "auto"}, overrides=list(overrides))


def logs(title, expr, w=24, h=12):
    return panel("logs", title, [target(expr, ds=LOKI)], w, h, ds=LOKI,
                 options={"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                          "enableLogDetails": True, "prettifyLogMessage": False})


def row(title, collapsed=False):
    return {"type": "row", "title": title, "collapsed": collapsed, "panels": [],
            "gridPos": {"w": 24, "h": 1}}


def column_override(name, **props):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": k, "value": v} for k, v in props.items()]}


def cell_color(mode="color-background"):
    return {"type": mode}


def layout(items):
    """Assign x/y left to right, wrapping at 24 columns; rows start a new line."""
    x = y = line_h = 0
    for p in items:
        g = p["gridPos"]
        if p["type"] == "row" or x + g["w"] > 24:
            y += line_h
            x = line_h = 0
        g["x"], g["y"] = x, y
        if p["type"] == "row":
            y += 1
            continue
        x += g["w"]
        line_h = max(line_h, g["h"])
    for i, p in enumerate(items, 1):
        p["id"] = i
    return items


def query_var(name, label, query, multi=False, include_all=False, hide=0, regex=""):
    return {"type": "query", "name": name, "label": label, "datasource": PROM,
            "query": {"qryType": 1, "query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
            "definition": query, "refresh": 2, "sort": 1, "multi": multi, "includeAll": include_all,
            "hide": hide, "regex": regex, "current": {}, "options": []}


def dashboard(uid, title, panels, variables=(), links=(), time="now-24h", desc=""):
    return {
        "uid": uid,
        "title": title,
        "description": desc,
        "tags": ["home-ops"],
        "editable": False,
        "graphTooltip": 1,
        "timezone": "browser",
        "refresh": "1m",
        "time": {"from": time, "to": "now"},
        "templating": {"list": list(variables)},
        "annotations": {"list": []},
        "links": list(links),
        "panels": layout(panels),
        "schemaVersion": 41,
        "version": 1,
    }


SERVICE_LINK = "/d/service/service?var-stack=${__data.fields.stack}&var-container=${__value.raw}&${__url_time_range}"
DASH_LINKS = [{"type": "dashboards", "tags": ["home-ops"], "asDropdown": True, "title": "home-ops",
               "includeVars": False, "keepTime": True}]


# ---------------------------------------------------------------------------- Tier overview

def tier_rows():
    out = []
    for tier in TIERS:
        scope = f'tier="{tier}"'
        members = f'group by (container) (service:uptime:ok{{{scope}}})'
        p95 = ("histogram_quantile(0.95, sum by (container, le) ("
               + TRAEFIK_CONTAINER.format(expr="rate(traefik_service_request_duration_seconds_bucket[10m])")
               + f")) and on (container) {members}")
        out += [
            row(f"{tier.capitalize()} Services"),
            table(f"{tier.capitalize()}: now", [
                target(f"max by (container, stack) (service:uptime:ok{{{scope}}})", ref="A", instant=True, table=True),
                target(f"max by (container) (service:availability:ratio1d{{{scope}}}) and on (container) {members}", ref="B", instant=True, table=True),
                target(f"container:traefik_service_requests:rate10m and on (container) {members}", ref="C", instant=True, table=True),
                target(f"container:traefik_service_5xx:ratio10m and on (container) {members}", ref="D", instant=True, table=True),
                target(p95, ref="E", instant=True, table=True),
            ], {"Value #A": "Uptime", "Value #B": "Availability 1d", "Value #C": "req/s",
                "Value #D": "5xx", "Value #E": "p95"},
                w=12, h=10, sort="Uptime",
                desc="Uptime now, rolling 1d Availability, and Traefik traffic over 10m. Click a Service for its dashboard.",
                overrides=[
                    column_override("container", displayName="Service",
                                    links=[{"title": "Service dashboard", "url": SERVICE_LINK}]),
                    column_override("Uptime", mappings=UP_MAPPING, **{"custom.cellOptions": cell_color()}),
                    column_override("Availability 1d", unit="percentunit", decimals=2,
                                    thresholds={"mode": "absolute", "steps": AVAILABILITY_STEPS},
                                    **{"custom.cellOptions": cell_color("color-text")}),
                    column_override("req/s", unit="reqps", decimals=2),
                    column_override("5xx", unit="percentunit", decimals=1),
                    column_override("p95", unit="s", decimals=2),
                ]),
            state_timeline(f"{tier.capitalize()}: Uptime", [
                target(f"max by (container) (service:uptime:ok{{{scope}}})", "{{container}}")], w=12, h=10),
        ]
    return out


def overview():
    firing = 'ALERTS{alertstate="firing", alertname!="Watchdog"'
    panels = [
        stat("Services down", "count(service:uptime:ok == 0) or vector(0)",
             steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
             desc="Services whose Uptime is 0 right now (failed Probe, or unhealthy health check)."),
        stat("Availability 1d (mean)", "avg(service:availability:ratio1d)", unit="percentunit",
             decimals=2, steps=AVAILABILITY_STEPS),
        stat("Internet", "max(probe_success{job=\"internet\"})", mappings=UP_MAPPING,
             steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
        *[stat(f"Firing: {t}", f'count({firing}, tier="{t}"}}) or vector(0)',
               steps=[{"color": "green", "value": None},
                      {"color": {"critical": "red", "warning": "orange", "info": "blue"}[t], "value": 1}])
          for t in TIERS],
        *tier_rows(),
        row("Dependencies"),
        state_timeline("Dependency Probes", [
            target('min by (from, to) (probe_success{job="dependency"})', "{{from}} → {{to}}")], w=12, h=8,
            desc="Each edge in prometheus/dependencies.yml, probed with the URL the dependent uses."),
        timeseries("Dependency Probe duration", [
            target('max by (from, to) (probe_duration_seconds{job="dependency"})', "{{from}} → {{to}}")],
            unit="s"),
        row("Alerts"),
        table("Firing alerts", [target(f"max by (alertname, tier, stack, container) ({firing}}})",
                                       instant=True, table=True)],
              {"Value": "Firing"}, w=24, h=8, join="alertname",
              overrides=[column_override("Firing", **{"custom.hidden": True})]),
    ]
    return dashboard("overview", "Overview", panels, links=DASH_LINKS,
                     desc="Every Service by Tier: Uptime, Availability, and traffic through Traefik.")


# ---------------------------------------------------------------------------- Service

def service():
    c = 'container="$container"'
    name = 'name="$container"'
    tsvc = 'service="$container@docker"'
    variables = [
        query_var("stack", "Stack", "label_values(container_last_seen, container_label_com_docker_compose_project)"),
        query_var("container", "Service",
                  'label_values(container_last_seen{container_label_com_docker_compose_project="$stack"}, name)',
                  regex="/^(?![0-9a-f]{12}_).*/"),
    ]
    rate = "$__rate_interval"
    panels = [
        row("Uptime"),
        stat("Uptime", f"max(service:uptime:ok{{{c}}})", mappings=UP_MAPPING,
             steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
             desc="Probe success for an Exposed Service, otherwise its own health check."),
        stat("Availability 1d", f"max(service:availability:ratio1d{{{c}}})", unit="percentunit",
             decimals=3, steps=AVAILABILITY_STEPS),
        stat("Availability (range)", f"avg(avg_over_time(service:uptime:ok{{{c}}}[$__range]))",
             unit="percentunit", decimals=3, steps=AVAILABILITY_STEPS),
        stat("Health check", f"max(container_healthy{{{c}}})", mappings=UP_MAPPING,
             steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
             desc="The container's own health check (empty if it has none)."),
        stat("Restarts (range)", f"sum(changes(container_start_time_seconds{{{name}}}[$__range]))",
             steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]),
        stat("Firing alerts", f'count(ALERTS{{alertstate="firing", {c}}}) or vector(0)',
             steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
        state_timeline("Probes", [
            target(f"max by (job, instance) (probe_success{{{c}}})", "{{job}} {{instance}}", "A"),
            target(f"max(container_healthy{{{c}}})", "health check", "B"),
        ], w=12, h=7),
        timeseries("Probe duration", [
            target(f"max by (job, instance) (probe_duration_seconds{{{c}}})", "{{job}} {{instance}}")],
            h=7, unit="s"),

        row("HTTP (through Traefik)"),
        timeseries("Requests by status", [
            target(f"sum by (code) (rate(traefik_service_requests_total{{{tsvc}}}[{rate}]))", "{{code}}")],
            w=8, unit="reqps", stack=True),
        timeseries("5xx ratio", [
            target(f'(sum(rate(traefik_service_requests_total{{{tsvc}, code=~"5..", code!="501"}}[{rate}])) or vector(0))'
                   f" / sum(rate(traefik_service_requests_total{{{tsvc}}}[{rate}]))", "5xx")],
            w=8, unit="percentunit", desc="Http5xxRatio alerts above 5% for 10m (501 excluded)."),
        timeseries("Latency", [
            target(f"histogram_quantile({q}, sum by (le) (rate(traefik_service_request_duration_seconds_bucket{{{tsvc}}}[{rate}])))",
                   f"p{int(q * 100)}", ref)
            for q, ref in ((0.5, "A"), (0.95, "B"), (0.99, "C"))], w=8, unit="s"),

        row("Resources"),
        timeseries("CPU", [target(f"sum(rate(container_cpu_usage_seconds_total{{{name}}}[{rate}]))", "cores")],
                   w=8, unit="short", desc="CPU cores in use (1 = one full core)."),
        timeseries("Memory", [
            target(f"sum(container_memory_working_set_bytes{{{name}}})", "working set", "A"),
            target(f"sum(container_spec_memory_limit_bytes{{{name}}}) > 0", "limit", "B"),
        ], w=8, unit="bytes"),
        timeseries("Network", [
            target(f"sum(rate(container_network_receive_bytes_total{{{name}}}[{rate}]))", "receive", "A"),
            target(f"-sum(rate(container_network_transmit_bytes_total{{{name}}}[{rate}]))", "transmit", "B"),
        ], w=8, unit="Bps", desc="Disk I/O per container isn't attributable on ZFS; see the Host dashboard."),

        row("Dependencies"),
        state_timeline("Depends on", [
            target('min by (to, kind) (probe_success{job="dependency", from="$container"})', "{{to}} ({{kind}})")],
            w=12, h=6, desc="Dependency Probes from this Service's configured URLs (prometheus/dependencies.yml)."),
        state_timeline("Depended on by", [
            target('min by (from) (probe_success{job="dependency", to="$container"})', "{{from}}")], w=12, h=6),

        row("Metrics and alerts"),
        table("Scrape targets in $stack", [
            target('max by (job, instance) (up{stack="$stack", job!~"probe.*|dependency|internet"})', instant=True, table=True)],
            {"Value": "up"}, w=12, h=6, join="job",
            overrides=[column_override("up", mappings=UP_MAPPING, **{"custom.cellOptions": cell_color()})]),
        table("Firing alerts", [
            target(f'max by (alertname, tier) (ALERTS{{alertstate="firing", {c}}})', instant=True, table=True)],
            {"Value": "Firing"}, w=12, h=6, join="alertname",
            overrides=[column_override("Firing", **{"custom.hidden": True})]),

        row("Logs"),
        timeseries("Log lines", [
            target(f'sum by (logstream) (count_over_time({{{c}}}[$__auto]))', "{{logstream}}", ds=LOKI)],
            w=24, h=6, ds=LOKI, stack=True, unit="short"),
        logs("Logs", f"{{{c}}}"),
    ]
    return dashboard("service", "Service", panels, variables, links=DASH_LINKS, time="now-6h",
                     desc="One Service: Uptime, HTTP, resources, Dependencies, alerts and logs.")


DASHBOARDS = {"overview": overview, "service": service}


def render():
    return {f"{name}.json": json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
            for name, build in DASHBOARDS.items()}


def main():
    files = render()
    if "--check" in sys.argv:
        stale = [n for n, text in files.items()
                 if not (OUT / n).exists() or (OUT / n).read_text() != text]
        extra = sorted(p.name for p in OUT.glob("*.json") if p.name not in files)
        for n in stale:
            print(f"{OUT / n}: stale; run python3 tn/monitoring/grafana/dashboards.py")
        for n in extra:
            print(f"{OUT / n}: not produced by the generator")
        sys.exit(1 if stale or extra else 0)
    OUT.mkdir(exist_ok=True)
    for n, text in files.items():
        (OUT / n).write_text(text)


if __name__ == "__main__":
    main()
