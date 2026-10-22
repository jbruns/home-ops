"""The silences `hops stop` creates: each label set by which an alert can name a stopped Service.

A silence's matchers must all match, and the alerts a stopped Service sets off name it in different
ways (alerting-rules.yml, prometheus.yml):

- `stack` and `service`: ContainerDown, and Probes (ProbeFailed), which relabel from Compose.
- `container`: scrape targets (TargetDown), Probes, Http5xxRatio, internal DependencyDown, the IdP
  alerts. A static target's `container` label is the hostname it is scraped at, which for a Service
  without container_name is its Service name, not the container's.
- `name`: cAdvisor's container alerts, ContainerDown, and DatastoreDown (copied from `datastore`).
- `datastore`: what an exporter's target says about the Datastore it watches (DatastoreDown).

Alerts that fan out from a stopped Service to others (every Probe while Traefik is down) are left to
Alertmanager's inhibit rules, which still apply while their source alert is silenced.
"""
from pathlib import Path

import yaml

from hops.declared import Service

SCRAPE_CONFIG = Path("monitoring") / "prometheus" / "prometheus.yml"  # under the Stack root


def scrape_targets(repo_root: Path, stack_root: str) -> list[tuple[str, dict]]:
    """(address, labels) of every static scrape target; none if the repo has no scrape config."""
    path = repo_root / stack_root / SCRAPE_CONFIG
    if not path.is_file():
        return []
    config = yaml.safe_load(path.read_text()) or {}
    return [(str(address), static.get("labels") or {})
            for sc in config.get("scrape_configs") or []
            for static in sc.get("static_configs") or []
            for address in static.get("targets") or []]


def matchers(service: Service, container: str, targets: list[tuple[str, dict]]) -> list[dict[str, str]]:
    """Equality matcher sets, one per silence, for a Service whose container is named `container`."""
    found = [{"stack": service.stack, "service": service.name}, {"container": container}, {"name": container}]
    hostnames = {container, service.name, service.container_name} - {None}
    for address, labels in targets:
        if address.rsplit(":", 1)[0] in hostnames and labels.get("container"):
            found.append({"container": labels["container"]})
        if labels.get("datastore") in hostnames:
            found.append({"datastore": labels["datastore"]})
    unique = []
    for m in found:
        if m not in unique:
            unique.append(m)
    return unique
