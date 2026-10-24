#!/usr/bin/env python3
"""Cloudflare edge ranges that Traefik trusts for X-Forwarded-For.

The ranges appear three times in the Traefik Compose file (both entrypoints and the
CrowdSec bouncer). `homelab-validate cloudflare` (shared Repo Tooling) checks they agree;
`--update` rewrites all three from Cloudflare's API when the published set changes (run
weekly by .github/workflows/cloudflare-ips.yaml), and refuses a file whose lists are
already malformed or differ.

Usage: cloudflare_ips.py --update [--file PATH]   (default: tn/edge/traefik/service.yaml)
"""
import argparse
import ipaddress
import json
import re
import sys
import urllib.request
from pathlib import Path

TRAEFIK = Path(__file__).resolve().parents[2] / "tn/edge/traefik/service.yaml"
API = "https://api.cloudflare.com/client/v4/ips"
LIST_RE = re.compile(r"(forwardedHeaders\.trustedIPs=|forwardedHeadersTrustedIPs: )(\S+)")
EXPECTED_LISTS = 3


def extract(text: str) -> list[list[str]]:
    return [m.group(2).split(",") for m in LIST_RE.finditer(text)]


def problems(text: str) -> list[str]:
    lists = extract(text)
    if len(lists) != EXPECTED_LISTS:
        return [f"expected {EXPECTED_LISTS} Cloudflare range lists, found {len(lists)}"]
    out = []
    for cidr in lists[0]:
        try:
            ipaddress.ip_network(cidr)
        except ValueError:
            out.append(f"invalid CIDR {cidr!r}")
    if any(set(lst) != set(lists[0]) for lst in lists[1:]):
        out.append("the Cloudflare range lists differ; keep all three identical")
    return out


def fetch() -> list[str]:
    with urllib.request.urlopen(API, timeout=30) as r:
        body = json.load(r)
    result = body.get("result") or {}
    cidrs = [*result.get("ipv4_cidrs", []), *result.get("ipv6_cidrs", [])]
    if not body.get("success") or not cidrs:
        sys.exit(f"unexpected response from {API}: {body}")
    for cidr in cidrs:
        ipaddress.ip_network(cidr)
    return cidrs


def update(path: Path = TRAEFIK) -> bool:
    text = path.read_text()
    if errs := problems(text):
        sys.exit("; ".join(errs))
    latest = fetch()
    if set(latest) == set(extract(text)[0]):
        print("Cloudflare ranges unchanged")
        return False
    path.write_text(LIST_RE.sub(lambda m: m.group(1) + ",".join(latest), text))
    print(f"Cloudflare ranges updated: {len(latest)} CIDRs")
    return True


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Sync Traefik's trusted Cloudflare ranges with Cloudflare's API.")
    parser.add_argument("--update", action="store_true", help="rewrite the ranges if Cloudflare's set changed")
    parser.add_argument("--file", type=Path, default=TRAEFIK, help=f"Compose file holding the ranges (default: {TRAEFIK})")
    args = parser.parse_args(argv)
    if not args.update:
        sys.exit("usage: cloudflare_ips.py --update [--file PATH]")
    update(args.file)


if __name__ == "__main__":
    main(sys.argv[1:])
