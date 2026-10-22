"""The silences `hops stop` creates, derived from the repo's monitoring config.

A silence's matchers must all match, so one Service needs several, one per way an alert can name
it (alerting-rules.yml, prometheus.yml):

- `stack` and `service`: ContainerDown, and Probes (ProbeFailed), which relabel from Compose.
- `container`: scrape targets (TargetDown), Probes, Http5xxRatio, internal DependencyDown, the IdP
  alerts. A static target's `container` label is the hostname it is scraped at, which for a Service
  without container_name is its Service name, not the container's.
- `name`: cAdvisor's container alerts, ContainerDown, and DatastoreDown (copied from `datastore`).
- `datastore`: what an exporter's target says about the Datastore it watches (DatastoreDown).

A stopped Service also sets off alerts that name others:

- Services in its Stack that `depends_on` it, directly or not, fail too: each gets the same set.
- An inhibit rule whose source is the stopped Service's own ProbeFailed (Traefik's, which inhibits
  every other Probe) stops working: Docker SD drops a stopped container's Probe target, so the
  source never fires. What it inhibited is silenced instead.
- Rules that label an alert with the Stack but name no Service (BackupStale is `stack="ops"`) get
  `alertname` and `stack`. Only Paging and Digest Tiers: the Watchdog heartbeat is never silenced.

Other inhibitions still apply, since a silenced source alert still inhibits.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hops.declared import Declared, Service

MONITORING = Path("monitoring")  # under the Stack root
SCRAPE_CONFIG = MONITORING / "prometheus" / "prometheus.yml"
ALERT_RULES = MONITORING / "prometheus" / "alerting-rules.yml"
ALERTMANAGER = MONITORING / "alertmanager" / "config.yml"
# Alerts from docker_sd targets, which vanish with the container, so they never fire for a stopped one.
SD_ALERTS = {"ProbeFailed"}
IDENTITY = ("service", "container", "name")
NOTIFYING = {"critical", "warning"}
MATCHER = re.compile(r'\s*(\w+)\s*(=~|!~|!=|=)\s*"(.*)"\s*')


@dataclass(frozen=True)
class Silence:
    matchers: dict[str, str]  # label = value, all equality
    reason: str
    origin: Service  # the stopped Service it is for


@dataclass
class Monitoring:
    """What the repo says about alerts: static scrape targets, alert rules, inhibit rules."""
    targets: list[tuple[str, dict]] = field(default_factory=list)  # (address, labels)
    rules: dict[str, dict] = field(default_factory=dict)  # alertname -> static labels
    inhibitions: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, repo_root: Path, stack_root: str) -> "Monitoring":
        """Missing files (a repo without the monitoring Stack) give no targets, rules or inhibitions."""
        def read(path):
            path = repo_root / stack_root / path
            return (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}

        targets = [(str(address), static.get("labels") or {})
                   for sc in read(SCRAPE_CONFIG).get("scrape_configs") or []
                   for static in sc.get("static_configs") or []
                   for address in static.get("targets") or []]
        rules = {r["alert"]: r.get("labels") or {}
                 for g in read(ALERT_RULES).get("groups") or [] for r in g.get("rules") or [] if "alert" in r}
        return cls(targets, rules, read(ALERTMANAGER).get("inhibit_rules") or [])


def equalities(matchers: list[str]) -> dict[str, str] | None:
    """The `label="value"` matchers of an Alertmanager list, or None if any matcher is not one."""
    found = {}
    for text in matchers:
        m = MATCHER.fullmatch(text)
        if not m or m[2] != "=":
            return None
        found[m[1]] = m[3]
    return found


def own(service: Service, container: str | None, monitoring: Monitoring, why: str = "") -> list[tuple[dict, str]]:
    """Each label set by which an alert names the Service; `container` None when it is not known."""
    found = [({"stack": service.stack, "service": service.name}, "ContainerDown and its Probes")]
    if container:
        found += [({"container": container}, "alerts on its container"),
                  ({"name": container}, "container alerts by name")]
    hostnames = {container, service.name, service.container_name} - {None}
    for address, labels in monitoring.targets:
        if address.rsplit(":", 1)[0] in hostnames and labels.get("container"):
            found.append(({"container": labels["container"]}, f"TargetDown of scrape target {address}"))
        if labels.get("datastore") in hostnames:
            found.append(({"datastore": labels["datastore"]}, "DatastoreDown from its exporter"))
    return [(m, f"{why}{service.key}: {reason}") for m, reason in found]


def dependents(declared: Declared, service: Service) -> list[Service]:
    """Services in the same Stack that depend_on it, directly or through each other."""
    found, todo = [], [service.name]
    while todo:
        name = todo.pop(0)
        for s in declared.stacks.get(service.stack, []):
            if name in (s.spec.get("depends_on") or []) and s != service and s not in found:
                found.append(s)
                todo.append(s.name)
    return found


def lost_inhibitions(container: str, monitoring: Monitoring) -> list[tuple[dict, str]]:
    """Silences for what the stopped container's own docker_sd alert would have inhibited."""
    found = []
    for rule in monitoring.inhibitions:
        source = equalities(rule.get("source_matchers") or [])
        if not source or source.get("alertname") not in SD_ALERTS or source.get("container") != container:
            continue
        target = equalities([m for m in rule.get("target_matchers") or [] if "alertname" in m]) or {}
        alertname = target.get("alertname")
        if not alertname:
            continue
        static = monitoring.rules.get(alertname, {})
        matchers = {"alertname": alertname, **{k: v for k, v in static.items()
                                                if k in ("stack", *IDENTITY) and "{{" not in str(v)}}
        what = "covers all Probes, which route through it" if alertname == "ProbeFailed" else f"covers {alertname}"
        found.append((matchers, f"{container}: {what}; its own {source['alertname']} would inhibit them, but "
                                f"never fires once Docker SD drops its target"))
    return found


def stack_scoped(stack: str, monitoring: Monitoring) -> list[tuple[dict, str]]:
    """Notifying alerts labelled with the Stack that name no Service."""
    return [({"alertname": name, "stack": stack}, f"{stack}: {name} names the Stack but no Service")
            for name, labels in monitoring.rules.items()
            if labels.get("stack") == stack and labels.get("tier") in NOTIFYING
            and not any(k in labels for k in IDENTITY)]


def plan(declared: Declared, stopped: list[tuple[Service, str]], monitoring: Monitoring) -> list[Silence]:
    """Every silence for stopping these (Service, container name) pairs, without duplicates."""
    found: list[Silence] = []
    for service, container in stopped:
        sets = own(service, container, monitoring)
        for d in dependents(declared, service):
            sets += own(d, d.container_name, monitoring, why=f"depends on {service.name}, ")
        sets += lost_inhibitions(container, monitoring)
        sets += stack_scoped(service.stack, monitoring)
        for matchers, reason in sets:
            if all(s.matchers != matchers for s in found):
                found.append(Silence(matchers, reason, service))
    return found
