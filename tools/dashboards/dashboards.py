#!/usr/bin/env python3
"""Generate the Grafana dashboards provisioned from the repo (stdlib only).

    python3 tools/dashboards/dashboards.py           # write tn/monitoring/grafana/dashboards/*.json
    python3 tools/dashboards/dashboards.py --check   # fail if they are stale (validate.py)
    python3 tools/dashboards/dashboards.py --out DIR # write or check another folder

Edit this file, never the JSON. Vocabulary follows GLOSSARY.md: Service, Stack, Tier, Probe,
Uptime, Availability, Dependency. Datasources are referenced by the uids set in
tn/monitoring/grafana/provisioning/datasources.yml.
"""
import argparse
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "tn/monitoring/grafana/dashboards"

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
REMOTE = {"type": "prometheus", "uid": "prometheus-remote"}  # nzbhydra2's machine (ADR 0005)
MACHINE = {"type": "prometheus", "uid": "${ds}"}

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


def stat(title, expr, w=4, h=4, unit=None, mappings=None, steps=None, desc=None, decimals=None, ds=PROM):
    d = {}
    if mappings:
        d["mappings"] = mappings
    if decimals is not None:
        d["decimals"] = decimals
    # Without steps, Grafana's default thresholds paint any value of 80 or more red.
    d["thresholds"] = {"mode": "absolute", "steps": steps or [{"color": "text", "value": None}]}
    return panel("stat", title, [target(expr, instant=True, ds=ds)], w, h, unit, desc, ds=ds, defaults=d,
                 options={"colorMode": "background" if steps or mappings else "none", "graphMode": "none", "textMode": "value",
                          "reduceOptions": {"calcs": ["lastNotNull"]}})


def timeseries(title, targets, w=12, h=8, unit=None, desc=None, stack=False, custom=None, **extra):
    base = {"fillOpacity": 10, "showPoints": "never", "lineWidth": 1}
    if stack:
        base |= {"stacking": {"mode": "normal"}, "fillOpacity": 40}
    return panel("timeseries", title, targets, w, h, unit, desc, custom=base | (custom or {}),
                 options={"legend": {"displayMode": "list", "placement": "bottom"},
                          "tooltip": {"mode": "multi", "sort": "desc"}}, **extra)


UP_STEPS = [{"color": "red", "value": None}, {"color": "green", "value": 1}]


def state_timeline(title, targets, w=12, h=8, mappings=UP_MAPPING, desc=None, ds=PROM, steps=UP_STEPS):
    return panel("state-timeline", title, targets, w, h, desc=desc, ds=ds,
                 defaults={"mappings": mappings, "color": {"mode": "thresholds"},
                           "thresholds": {"mode": "absolute", "steps": steps}},
                 custom={"fillOpacity": 80, "lineWidth": 0},
                 options={"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                          "legend": {"showLegend": False}})


def table(title, targets, columns, w=12, h=8, join="container", overrides=(), desc=None, sort=None, ds=PROM):
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
    return panel("table", title, targets, w, h, desc=desc, ds=ds, transformations=transforms,
                 options=opts, custom={"align": "auto"}, overrides=list(overrides))


def logs(title, expr, w=24, h=12, desc=None):
    return panel("logs", title, [target(expr, ds=LOKI)], w, h, desc=desc, ds=LOKI,
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


def query_var(name, label, query, multi=False, include_all=False, hide=0, regex="", ds=PROM):
    return {"type": "query", "name": name, "label": label, "datasource": ds,
            "query": {"qryType": 1, "query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
            "definition": query, "refresh": 2, "sort": 1, "multi": multi, "includeAll": include_all,
            "hide": hide, "regex": regex, "current": {}, "options": []}


def datasource_var(name, label, regex):
    """Matches datasource names, so the JSON never names a machine."""
    return {"type": "datasource", "name": name, "label": label, "query": "prometheus", "regex": regex,
            "refresh": 1, "hide": 0, "multi": False, "includeAll": False, "current": {}, "options": []}


def node_graph(title, nodes_expr, edges_expr, w=24, h=14, desc=None):
    """Instant queries with refIds `nodes`/`edges`. Each series' labels become columns: `id`, `title`,
    `subtitle` for nodes; `id`, `source`, `target` for edges. Each refId is then merged into one frame,
    because Prometheus returns one frame per series. Node graph ignores field config (mappings,
    thresholds), so the 1/0 value becomes `mainstat` and `color` labels holding the text and CSS colour."""
    def with_state(expr):
        return " or ".join(
            f'label_replace(label_replace(({expr}) == {v}, "mainstat", "{text}", "", ""), "color", "{color}", "", "")'
            for v, text, color in ((1, "UP", "#73BF69"), (0, "DOWN", "#F2495C")))

    targets = [target(with_state(nodes_expr), ref="nodes", instant=True),
               target(with_state(edges_expr), ref="edges", instant=True)]
    transforms = [
        {"id": "labelsToFields", "options": {"mode": "columns"}},
        *({"id": "merge", "options": {}, "filter": {"id": "byRefId", "options": ref}} for ref in ("nodes", "edges")),
        {"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True}}},
    ]
    return panel("nodeGraph", title, targets, w, h, desc=desc, transformations=transforms,
                 options={"layoutAlgorithm": "layered", "zoomMode": "cooperative"})


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
        h = 18 if tier == "warning" else 10  # warning holds most Services
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
                w=12, h=h, sort="Uptime",
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
                target(f"max by (container) (service:uptime:ok{{{scope}}})", "{{container}}")], w=12, h=h),
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

        # Router metrics, unlike the service metrics above, also count what a middleware answers
        # (forward auth's 302/401, the CrowdSec bouncer's 403). route_kind: recording-rules.yml.
        row("4xx (through Traefik routers)"),
        timeseries("4xx by route kind", [
            target(f"sum by (route_kind) (container_route:traefik_router_4xx:rate10m{{{c}}})", "{{route_kind}}")],
            w=8, unit="reqps", stack=True,
            desc="ui: the web UI (forward auth and CrowdSec answer here too); client: Client Paths; "
                 "health: the Probe path; http: the redirect to https. A basic-auth Service's ui 401s are mostly its own Probe."),
        timeseries("4xx by router and code", [
            target(f"sum by (router, code) (container_route:traefik_router_4xx:rate10m{{{c}}})", "{{router}} {{code}}")],
            w=8, unit="reqps",
            desc="401s on a -client-<protocol> router are failed sign-ins: a user's, or brute force. "
                 "403s are mostly the CrowdSec bouncer."),
        timeseries("Requests by route kind", [
            target(f"sum by (route_kind, code_class) (container_route:traefik_router_requests:rate10m{{{c}}})",
                   "{{route_kind}} {{code_class}}")],
            w=8, unit="reqps", stack=True),
        logs("4xx from home addresses",
             '{container="traefik", logstream="access", client="home"} | json'
             ' | DownstreamStatus >= 400 and DownstreamStatus < 500'
             ' and RouterName =~ "$container-(https?|health|client-[a-z0-9]+)@.+"'
             ' and request_User_Agent !~ "Blackbox-Exporter/.*"'
             ' | line_format "{{.DownstreamStatus}} {{.RequestMethod}} {{.RequestPath}} via {{.RouterName}} ({{.request_User_Agent}})"',
             w=16, h=10,
             desc="A home user's failures, from the home public addresses (the CrowdSec whitelist; Alloy labels them "
                  "client=home). Probes are excluded. Client IPs are only in the access log, never in metrics."),
        timeseries("CrowdSec decisions in effect", [
            target('sum by (reason) (cs_active_decisions{origin="crowdsec"})', "{{reason}}")],
            w=8, h=10, stack=True,
            desc="Host-wide bans from this Host's own scenarios (community blocklists excluded), "
                 "for comparing Internet noise with the home 4xx beside it."),

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


# ---------------------------------------------------------------------------- Machine

def machine():
    n = 'job="$node"'
    disks = 'device=~"sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+"'
    virtual = "lo|veth.*|br-.*|docker.*|vnet.*|tap.*|hassio"
    # cAdvisor's root cgroup sees the machine's interfaces; our node-exporters only see their own
    # netns. cAdvisor and DCGM run beside job node-exporter only, so `docker` hides them for any
    # other node (Home Assistant), whose node-exporter shares the host netns.
    nics = f'job="cadvisor", id="/", interface!~"{virtual}"'
    docker = ' and on () group(node_uname_info{job="$node", job="node-exporter"})'
    host_nics = f'job="$node", job!="node-exporter", device!~"{virtual}"'
    rate = "$__rate_interval"
    mounts = "/|/mnt/[^/.][^/]*|/tmp"  # Home Assistant's node-exporter add-on sees its data partition as /tmp
    cores = f'scalar(count(node_cpu_seconds_total{{{n}, mode="idle"}}))'
    busy = [{"color": "green", "value": None}, {"color": "orange", "value": 0.8}, {"color": "red", "value": 0.95}]

    def t(expr, legend="", ref="A", **kw):
        return target(expr, legend, ref, ds=MACHINE, **kw)

    def ts(title, targets, **kw):
        return timeseries(title, targets, ds=MACHINE, **kw)

    panels = [
        row("Overview"),
        stat("Up for", f"time() - node_boot_time_seconds{{{n}}}", unit="dtdurations", ds=MACHINE),
        stat("CPU cores", f'count(node_cpu_seconds_total{{{n}, mode="idle"}})', ds=MACHINE),
        stat("Memory", f"node_memory_MemTotal_bytes{{{n}}}", unit="bytes", ds=MACHINE),
        stat("CPU busy", f'1 - avg(rate(node_cpu_seconds_total{{{n}, mode="idle"}}[5m]))',
             unit="percentunit", steps=busy, ds=MACHINE),
        stat("Memory used", f"1 - node_memory_MemAvailable_bytes{{{n}}} / node_memory_MemTotal_bytes{{{n}}}",
             unit="percentunit", steps=busy, ds=MACHINE,
             desc="ZFS ARC counts as used here; it shrinks under memory pressure (see Memory)."),
        stat("Containers", 'count(count by (name) (container_last_seen{job="cadvisor", name!=""}))' + docker, ds=MACHINE),

        row("CPU and memory"),
        ts("CPU by mode", [t(f'sum by (mode) (rate(node_cpu_seconds_total{{{n}, mode!="idle"}}[{rate}])) / {cores}',
                             "{{mode}}")], unit="percentunit", stack=True),
        ts("Load", [t(f"node_load{m}{{{n}}}", f"load{m}", ref) for m, ref in (("1", "A"), ("5", "B"), ("15", "C"))]
           + [t(cores, "cores", "D")]),
        ts("Memory", [
            t(f"node_memory_MemTotal_bytes{{{n}}} - node_memory_MemAvailable_bytes{{{n}}}", "used", "A"),
            t(f"node_zfs_arc_size{{{n}}}", "ZFS ARC (within used)", "B"),
            t(f"node_memory_Cached_bytes{{{n}}} + node_memory_Buffers_bytes{{{n}}}", "page cache", "C"),
            t(f"node_memory_MemTotal_bytes{{{n}}}", "total", "D"),
        ], unit="bytes"),
        ts("Pressure (PSI)", [
            t(f"rate(node_pressure_{r}_waiting_seconds_total{{{n}}}[{rate}])", r, ref)
            for r, ref in (("cpu", "A"), ("memory", "B"), ("io", "C"))],
           unit="percentunit", desc="Share of time some task waited on each resource."),

        row("Storage"),
        table("Filesystems", [
            t(f'max by (mountpoint) (node_filesystem_size_bytes{{{n}, fstype=~"zfs|xfs|ext4|btrfs", mountpoint=~"{mounts}"}})',
              ref="A", instant=True, table=True),
            t(f'max by (mountpoint) (node_filesystem_avail_bytes{{{n}, fstype=~"zfs|xfs|ext4|btrfs", mountpoint=~"{mounts}"}})',
              ref="B", instant=True, table=True),
            t(f'1 - max by (mountpoint) (node_filesystem_avail_bytes{{{n}, mountpoint=~"{mounts}"}})'
              f' / max by (mountpoint) (node_filesystem_size_bytes{{{n}, mountpoint=~"{mounts}"}})',
              ref="C", instant=True, table=True),
        ], {"Value #A": "Size", "Value #B": "Free", "Value #C": "Used"}, join="mountpoint", ds=MACHINE,
            desc="The root filesystem and each pool's top-level dataset (Home Assistant: its data partition).",
            overrides=[column_override("Size", unit="bytes"), column_override("Free", unit="bytes"),
                       column_override("Used", unit="percentunit", min=0, max=1,
                                       thresholds={"mode": "absolute", "steps": busy},
                                       **{"custom.cellOptions": {"type": "gauge", "mode": "basic"}})]),
        state_timeline("ZFS pools", [t(f'max by (zpool) (node_zfs_zpool_state{{{n}, state="online"}})', "{{zpool}}")],
                       ds=MACHINE, desc="UP = ONLINE. Empty on machines without ZFS."),
        ts("Disk busy", [t(f"rate(node_disk_io_time_seconds_total{{{n}, {disks}}}[{rate}])", "{{device}}")],
           unit="percentunit", desc="Per disk: share of time with I/O in flight."),
        ts("Disk throughput", [
            t(f"sum(rate(node_disk_read_bytes_total{{{n}, {disks}}}[{rate}]))", "read", "A"),
            t(f"-sum(rate(node_disk_written_bytes_total{{{n}, {disks}}}[{rate}]))", "write", "B"),
        ], unit="Bps"),
        ts("ZFS ARC hit ratio", [
            t(f"sum(rate(node_zfs_arc_hits{{{n}}}[{rate}])) / (sum(rate(node_zfs_arc_hits{{{n}}}[{rate}]))"
              f" + sum(rate(node_zfs_arc_misses{{{n}}}[{rate}])))", "hit ratio")], unit="percentunit"),

        row("Network"),
        ts("Traffic by interface", [
            t(f"sum by (interface) (rate(container_network_receive_bytes_total{{{nics}}}[{rate}])){docker}", "{{interface}} in", "A"),
            t(f"-sum by (interface) (rate(container_network_transmit_bytes_total{{{nics}}}[{rate}])){docker}", "{{interface}} out", "B"),
            t(f"rate(node_network_receive_bytes_total{{{host_nics}}}[{rate}])", "{{device}} in", "C"),
            t(f"-rate(node_network_transmit_bytes_total{{{host_nics}}}[{rate}])", "{{device}} out", "D"),
        ], unit="Bps", desc="From cAdvisor's root cgroup (node-exporter for Home Assistant). A bridge and its NIC carry the same traffic."),
        ts("Errors and drops", [
            t(f"sum by (interface) (rate(container_network_{d}_{k}_total{{{nics}}}[{rate}])){docker}", f"{{{{interface}}}} {d} {k}", ref)
            for (d, k), ref in zip([(d, k) for d in ("receive", "transmit") for k in ("errors", "packets_dropped")], "ABCD")]
           + [t(f"rate(node_network_{d}_{k}_total{{{host_nics}}}[{rate}])", f"{{{{device}}}} {d} {k}", ref)
              for (d, k), ref in zip([(d, k) for d in ("receive", "transmit") for k in ("errs", "drop")], "EFGH")],
           unit="pps"),

        row("GPU"),
        ts("GPU utilisation", [t(f"max by (gpu) (DCGM_FI_DEV_{m}_UTIL){docker}", f"{m.lower()} {{{{gpu}}}}", ref)
                               for m, ref in (("GPU", "A"), ("ENC", "B"), ("DEC", "C"))],
           w=8, unit="percent", desc="NVIDIA DCGM; empty on machines without a GPU. enc/dec are Plex transcodes."),
        ts("GPU memory", [t("max by (gpu) (DCGM_FI_DEV_FB_USED)" + docker, "used {{gpu}}", "A"),
                          t("max by (gpu) (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE)" + docker, "total {{gpu}}", "B")],
           w=8, unit="decmbytes"),
        ts("GPU temperature and power", [t("max by (gpu) (DCGM_FI_DEV_GPU_TEMP)" + docker, "°C {{gpu}}", "A"),
                                         t("max by (gpu) (DCGM_FI_DEV_POWER_USAGE)" + docker, "W {{gpu}}", "B")], w=8),

        row("Temperatures"),
        ts("Sensors (max per chip)", [t(f"max by (chip) (node_hwmon_temp_celsius{{{n}}})", "{{chip}}")],
           w=24, unit="celsius"),

        row("Containers"),
        ts("Top 10 by CPU", [t(f'topk(10, sum by (name) (rate(container_cpu_usage_seconds_total{{job="cadvisor", name!=""}}[{rate}]))){docker}',
                               "{{name}}")], unit="short", desc="CPU cores in use."),
        ts("Top 10 by memory", [t('topk(10, sum by (name) (container_memory_working_set_bytes{job="cadvisor", name!=""}))' + docker,
                                  "{{name}}")], unit="bytes"),
    ]
    variables = [datasource_var("ds", "Prometheus", r"/^Prometheus( \(.+\))?$/"),
                 query_var("node", "Machine", "label_values(node_uname_info, job)", ds=MACHINE)]
    return dashboard("machine", "Machine", panels, variables, links=DASH_LINKS, time="now-24h",
                     desc="One machine's CPU, memory, storage, network, GPU and busiest containers.")


# ---------------------------------------------------------------------------- Dependency map

def dependencies():
    dep = 'job="dependency"'
    provider = ("max by (server) ((sabnzbd_usenet_server_error == bool 0)"
                " and on (server) (sabnzbd_usenet_server_active == 1))")

    def as_node(expr, src, subtitle):
        return (f'label_replace(label_replace({expr}, "id", "$1", "{src}", "(.+)"),'
                f' "subtitle", "{subtitle}", "", "")')

    def as_edge(expr, source, target_label):
        e = f'label_replace({expr}, "target", "$1", "{target_label}", "(.+)")'
        e = (f'label_replace({e}, "source", "$1", "from", "(.+)")' if source is None
             else f'label_replace({e}, "source", "{source}", "", "")')
        return f'label_join({e}, "id", " -> ", "source", "target")'

    dependents = f'group by (container) (label_replace(group by (from) (probe_success{{{dep}}}), "container", "$1", "from", "(.+)"))'
    internal = f'group by (container) (label_replace(group by (to) (probe_success{{{dep}, kind="internal"}}), "container", "$1", "to", "(.+)"))'
    nodes = (
        "max by (id, title, subtitle) (label_replace(\n  "
        + "\n  or ".join([
            as_node(f"max by (container) (service:uptime:ok) and on (container) ({dependents} or {internal})",
                    "container", "Service"),
            as_node(f'min by (to) (probe_success{{{dep}, kind="external"}})', "to", "External Dependency"),
            as_node(provider, "server", "Usenet provider"),
        ])
        + ',\n  "title", "$1", "id", "(.+)"))'
    )
    edges = (
        "max by (id, source, target) (\n  "
        + "\n  or ".join([
            as_edge(f"min by (from, to) (probe_success{{{dep}}})", None, "to"),
            as_edge(provider, "sabnzbd", "server"),
        ])
        + "\n)"
    )
    hydra_state = [{"type": "value", "options": {
        "1": {"text": "ENABLED", "color": "green", "index": 0},
        "0": {"text": "BACKOFF", "color": "orange", "index": 1},
        "-1": {"text": "DISABLED", "color": "red", "index": 2},
        "2": {"text": "OFF (user)", "color": "text", "index": 3},
    }}]
    rate = "$__rate_interval"
    # Only indexers with a limit configured in nzbhydra2 have a *_limit series, so only they appear.
    allowance = [
        target("max by (indexer) (nzbhydra2_indexer_api_hits / nzbhydra2_indexer_api_hit_limit)",
               "{{indexer}} API", "A", instant=True, ds=REMOTE),
        target("max by (indexer) (nzbhydra2_indexer_download_hits / nzbhydra2_indexer_download_hit_limit)",
               "{{indexer}} downloads", "B", instant=True, ds=REMOTE),
    ]
    allowance_steps = {"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "orange", "value": 0.9}, {"color": "red", "value": 1}]}
    allowance_desc = ("Hits as a share of each daily limit configured in nzbhydra2; indexers without one are not shown. "
                      "NzbhydraIndexerLimitNear alerts above 90% for 15 minutes. Counts reset at midnight.")
    panels = [
        row("Map"),
        node_graph("Dependency map", nodes, edges, h=16,
                   desc="Services with Dependency Probes, their Dependencies, and sabnzbd's Usenet providers. "
                        "Services show Uptime; External Dependencies show their worst Dependency Probe; edges show "
                        "the Dependency Probe (providers: active and no error in sabnzbd)."),

        row("Dependency Probes"),
        table("Dependency Probes", [
            target(f"max by (instance, from, to, kind) (probe_success{{{dep}}})", ref="A", instant=True, table=True),
            target(f"max by (instance) (probe_duration_seconds{{{dep}}})", ref="B", instant=True, table=True),
        ], {"Value #A": "Up", "Value #B": "Duration"}, join="instance", h=7,
            overrides=[column_override("Up", mappings=UP_MAPPING, **{"custom.cellOptions": cell_color()}),
                       column_override("Duration", unit="s")]),
        state_timeline("Dependency Probes over time", [
            target(f"min by (instance) (probe_success{{{dep}}})", "{{instance}}")], h=7),
        timeseries("Dependency Probe duration", [
            target(f"max by (instance) (probe_duration_seconds{{{dep}}})", "{{instance}}")], w=24, h=7, unit="s"),

        row("Usenet providers (sabnzbd)"),
        state_timeline("Providers", [target(provider, "{{server}}")], h=6,
                       desc="UP = active in sabnzbd with no error."),
        timeseries("Connections", [target("max by (server) (sabnzbd_usenet_server_connections)", "{{server}}")], h=6),

        row("nzbhydra2 indexers (nzbhydra2's own Prometheus)"),
        state_timeline("Indexer state", [target(
            'sum by (indexer) (nzbhydra2_indexer_state{state="ENABLED"} * 1'
            ' or nzbhydra2_indexer_state{state="DISABLED_SYSTEM"} * -1'
            ' or nzbhydra2_indexer_state{state="DISABLED_USER"} * 2)', "{{indexer}}", ds=REMOTE)],
            w=12, h=8, mappings=hydra_state, ds=REMOTE,
            steps=[{"color": "red", "value": None}, {"color": "orange", "value": 0}, {"color": "green", "value": 1}],
            desc="BACKOFF is nzbhydra2's temporary disable after errors. Gaps mean its machine is unreachable."),
        timeseries("Indexer API hits", [
            target("max by (indexer) (nzbhydra2_indexer_api_hits)", "{{indexer}}", "A", ds=REMOTE)],
            h=8, ds=REMOTE, desc="Every indexer, with or without a daily limit. Counts reset at midnight."),
        panel("bargauge", "Daily allowance used", allowance, 12, 8, "percentunit", ds=REMOTE,
              desc=allowance_desc, defaults={"min": 0, "max": 1, "thresholds": allowance_steps},
              options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                       "valueMode": "color", "reduceOptions": {"calcs": ["lastNotNull"]}}),
        timeseries("Daily allowance used over time", [dict(t, instant=False, range=True) for t in allowance], unit="percentunit",
                   h=8, ds=REMOTE, desc=allowance_desc,
                   defaults={"min": 0, "thresholds": allowance_steps},
                   custom={"thresholdsStyle": {"mode": "dashed"}}),
    ]
    return dashboard("dependencies", "Dependency map", panels, links=DASH_LINKS, time="now-24h",
                     desc="Internal and External Dependencies: the map, Dependency Probes, Usenet providers and indexers.")


# ---------------------------------------------------------------------------- Stacks

def service_links(*containers):
    return DASH_LINKS + [{"type": "link", "title": f"Service: {c}", "icon": "dashboard", "keepTime": True,
                          "url": f"/d/service/service?var-container={c}"} for c in containers]


def uptime_stat(container, w=4):
    return stat(f"{container}", f'max(service:uptime:ok{{container="{container}"}})', w=w,
                mappings=UP_MAPPING, steps=UP_STEPS, desc=f"{container}'s Uptime now.")


def count_steps(color):
    return [{"color": "green", "value": None}, {"color": color, "value": 1}]


def plex():
    # prometheus-plex-exporter swaps library/library_id/library_type on play_seconds_total;
    # media_type is reliable.
    playing = "rate(play_seconds_total[2m]) > 0"
    rate = "$__rate_interval"
    session_labels = ("user", "device", "media_type", "title", "grandchild_title", "stream_type",
                      "stream_resolution", "stream_file_resolution", "stream_bitrate")
    panels = [
        row("Now"),
        uptime_stat("plex"),
        stat("Streams", f"count({playing}) or vector(0)", desc="Sessions playing (paused sessions drop out)."),
        stat("Transcodes", f'count(({playing}) and on (session) play_seconds_total{{stream_type="transcode"}}) or vector(0)',
             steps=[{"color": "green", "value": None}, {"color": "orange", "value": 3}]),
        stat("Bandwidth out", "sum(rate(estimated_transmit_bytes_total[5m]))", unit="Bps",
             desc="Plex's own estimate of bytes sent to clients."),
        stat("Plex CPU", "max(host_cpu_util)", unit="percent", desc="Plex's view of its machine's CPU."),
        stat("Plex memory", "max(host_mem_util)", unit="percent", desc="Plex's view of its machine's memory."),

        row("Sessions"),
        table("Playing now", [target(f"max by (session, {', '.join(session_labels)}) ({playing})",
                                     instant=True, table=True)],
              {"Value": "rate"}, w=24, h=7, join="session",
              overrides=[column_override("rate", **{"custom.hidden": True}),
                         column_override("session", **{"custom.hidden": True}),
                         column_override("grandchild_title", displayName="episode"),
                         column_override("stream_bitrate", unit="Kbits")]),
        timeseries("Streams by type", [target(f"count by (stream_type) (rate(play_seconds_total[{rate}]) > 0)",
                                              "{{stream_type}}")], stack=True),
        timeseries("Bandwidth out", [target(f"sum(rate(estimated_transmit_bytes_total[{rate}]))", "sent")], unit="Bps"),
        timeseries("GPU encode/decode", [target(f"max(DCGM_FI_DEV_{m}_UTIL)", m.lower(), ref)
                                         for m, ref in (("ENC", "A"), ("DEC", "B"))],
                   unit="percent", desc="NVIDIA DCGM: hardware transcodes."),
        timeseries("Plex container", [
            target(f'sum(rate(container_cpu_usage_seconds_total{{job="cadvisor", name="plex"}}[{rate}]))', "CPU cores", "A"),
            target('sum(container_memory_working_set_bytes{job="cadvisor", name="plex"})', "memory", "B")],
            overrides=[{"matcher": {"id": "byName", "options": "memory"},
                        "properties": [{"id": "unit", "value": "bytes"},
                                       {"id": "custom.axisPlacement", "value": "right"}]}]),

        row("Watching"),
        table("Play time by user", [target(f"sum by (user) (increase(play_seconds_total[$__range]))",
                                           instant=True, table=True)],
              {"Value": "Played"}, w=6, join="user",
              overrides=[column_override("Played", unit="dtdurations")], desc="Over the dashboard's time range."),
        table("Play time by device", [target(f"sum by (device) (increase(play_seconds_total[$__range]))",
                                             instant=True, table=True)],
              {"Value": "Played"}, w=6, join="device", overrides=[column_override("Played", unit="dtdurations")]),
        table("Libraries", [
            target("max by (library) (library_storage_total)", ref="A", instant=True, table=True),
            target("max by (library) (library_duration_total)", ref="B", instant=True, table=True),
        ], {"Value #A": "Size", "Value #B": "Runtime"}, w=12, join="library",
            overrides=[column_override("Size", unit="bytes"), column_override("Runtime", unit="dtdurationms")]),
    ]
    return dashboard("plex", "Plex", panels, links=service_links("plex"), time="now-24h",
                     desc="Plex sessions, transcodes, bandwidth and libraries (prometheus-plex-exporter).")


def arr():
    rate = "$__rate_interval"
    apps = (("sonarr", "A"), ("radarr", "B"))

    def per_app(fmt, legend="{{app}}"):
        return [target(fmt.format(app=a), legend.replace("{{app}}", a), ref) for a, ref in apps]

    health = " or ".join(f'label_replace({a}_system_health_issues > 0, "app", "{a}", "", "")' for a, _ in apps)
    panels = [
        row("Status"),
        uptime_stat("sonarr", w=3), uptime_stat("radarr", w=3), uptime_stat("sabnzbd", w=3),
        stat("Health issues", "sum(sonarr_system_health_issues) + sum(radarr_system_health_issues)", w=3,
             steps=count_steps("orange"), desc="Issues on Sonarr and Radarr's System > Status page."),
        stat("Stuck in queue", "count(arr_queue_item_stuck) or vector(0)", w=3, steps=count_steps("red"),
             desc="Queue items waiting on an import, or flagged warning/error (arr-queue-exporter)."),
        stat("sabnzbd queue", "max(sabnzbd_queue_length)", w=3),
        stat("Download speed", "max(sabnzbd_speed_bps)", w=3, unit="Bps"),
        stat("sabnzbd", "max(sabnzbd_paused)", w=3, steps=count_steps("orange"),
             mappings=[{"type": "value", "options": {"0": {"text": "RUNNING", "index": 0},
                                                     "1": {"text": "PAUSED", "index": 1}}}]),
        table("Health issues", [target(f"max by (app, type, source, message) ({health})", instant=True, table=True)],
              {"Value": "n"}, w=12, h=6, join="message", overrides=[column_override("n", **{"custom.hidden": True})]),
        table("Stuck queue items", [target("max by (app, title, state, status, message) (arr_queue_item_info)",
                                           instant=True, table=True)],
              {"Value": "n"}, w=12, h=6, join="title", overrides=[column_override("n", **{"custom.hidden": True})]),

        row("Library"),
        stat("Series", "max(sonarr_series_total)", w=3),
        stat("Episodes", "max(sonarr_episode_downloaded_total)", w=3, desc="Downloaded episodes."),
        stat("Episodes missing", "max(sonarr_episode_missing_total)", w=3),
        stat("Episodes below cutoff", "max(sonarr_episode_cutoff_unmet_total)", w=3),
        stat("Movies", "max(radarr_movie_downloaded_total)", w=3, desc="Downloaded movies."),
        stat("Movies missing", "max(radarr_movie_missing_total)", w=3),
        stat("Movies wanted", "max(radarr_movie_wanted_total)", w=3),
        stat("Movies below cutoff", "max(radarr_movie_cutoff_unmet_total)", w=3),
        timeseries("Missing", [target("max(sonarr_episode_missing_total)", "episodes", "A"),
                               target("max(radarr_movie_missing_total)", "movies", "B")], w=8),
        timeseries("Activity", per_app("max(increase({app}_history_total[1h]))"), w=8,
                   desc="History entries per hour (grabs, imports, deletes)."),
        timeseries("Root folder free space", per_app("min by (path) ({app}_rootfolder_freespace_bytes)",
                                                      "{{app}} {{path}}"), w=8, unit="bytes"),

        row("Queue"),
        timeseries("Queue by state", [target("sum by (app, state) (arr_queue_items)", "{{app}} {{state}}")],
                   stack=True, desc="Sonarr and Radarr queue items by tracked download state."),
        timeseries("Stuck items", [target("count by (app) (arr_queue_item_stuck)", "{{app}}")]),

        row("sabnzbd"),
        timeseries("Download speed", [target("max(sabnzbd_speed_bps)", "speed")], unit="Bps"),
        timeseries("Downloaded by provider", [target(f"sum by (server) (rate(sabnzbd_server_downloaded_bytes[{rate}]))",
                                                     "{{server}}")], unit="Bps", stack=True,
                   desc="Provider health is on the Dependency map."),
        timeseries("Queue", [target("max(sabnzbd_remaining_bytes)", "remaining", "A"),
                             target("max(sabnzbd_queue_length)", "items", "B")],
                   overrides=[{"matcher": {"id": "byName", "options": "remaining"},
                               "properties": [{"id": "unit", "value": "bytes"}]},
                              {"matcher": {"id": "byName", "options": "items"},
                               "properties": [{"id": "custom.axisPlacement", "value": "right"}]}]),
        timeseries("Download folders free", [
            target("max by (folder) (sabnzbd_disk_total_bytes - sabnzbd_disk_used_bytes)", "{{folder}}")], unit="bytes"),
    ]
    links = service_links("sonarr", "radarr", "sabnzbd") + [
        {"type": "link", "title": "Dependency map", "icon": "dashboard", "keepTime": True, "url": "/d/dependencies"}]
    return dashboard("arr", "arr", panels, links=links, time="now-24h",
                     desc="Sonarr, Radarr and sabnzbd: health, library, queue and downloads (exportarr, arr-queue-exporter).")


DASHBOARDS = {"overview": overview, "service": service, "machine": machine, "dependencies": dependencies,
              "plex": plex, "arr": arr}


def render():
    return {f"{name}.json": json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
            for name, build in DASHBOARDS.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate the Grafana dashboards provisioned from the repo.")
    parser.add_argument("--check", action="store_true", help="fail if the JSON in --out is stale")
    parser.add_argument("--out", type=Path, default=OUT, help=f"dashboards folder (default: {OUT})")
    args = parser.parse_args(argv)
    out = args.out
    files = render()
    if args.check:
        stale = [n for n, text in files.items()
                 if not (out / n).exists() or (out / n).read_text() != text]
        extra = sorted(p.name for p in out.glob("*.json") if p.name not in files)
        for n in stale:
            print(f"{out / n}: stale; run python3 tools/dashboards/dashboards.py")
        for n in extra:
            print(f"{out / n}: not produced by the generator")
        sys.exit(1 if stale or extra else 0)
    out.mkdir(exist_ok=True)
    for n, text in files.items():
        (out / n).write_text(text)


if __name__ == "__main__":
    main()
