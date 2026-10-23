"""Recorded registry v2 exchanges for validate's anonymous-volume check, and the tools that record
and replay them. Unit tests never reach a registry.

Re-record (needs network); tokens are redacted:

    python3 tools/validate/tests/registry_fixtures.py [name ...]
"""
import json
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import validate  # noqa: E402
from hops.registry import Registry, Response, urllib_http  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
TOKEN = "anonymous-token"
KEPT_HEADERS = ("content-type", "docker-content-digest", "www-authenticate")
# name -> image as written in Compose (digest-pinned)
RECORDINGS = {
    # Docker Hub, an OCI index; the image declares VOLUME /attach /config /plugin.
    "dockerhub": "caronc/apprise:2.0.1@sha256:0b2913b15c8eb47c25345722985ffbfae701326edde21909c205d296d62dd52c",
    # Docker Hub, a single-platform manifest rather than an index.
    "dockerhub-single": "nextpvr/nextpvr_amd64:stable@sha256:79841be624d5719153ac7d2303c55fe671aa7d6feed3eaeb4020d5e418f00546",
    "ghcr": "ghcr.io/home-operations/sonarr:4.0.20.3012@sha256:1f19eb5e0f421418c1a956bbe01310a0141423afe28bd9a4b1dcb8629ff2bce2",
    "lscr": "lscr.io/linuxserver/qbittorrent:5.2.4@sha256:1ea873647c43d566f251a7122aee330b184509c57ee3257ed19f62dac0f8f24b",
    "quay": "quay.io/prometheus/node-exporter:v1.12.1@sha256:1b4e4438faca4dd7e001dd445d161a4a2091b0fededa84093b3a8dfeae1f1be0",
}


class Recorder:
    def __init__(self):
        self.exchanges = []

    def __call__(self, method, url, headers):
        resp = urllib_http(method, url, headers)
        body = resp.body.decode()
        if "/v2/" not in url and resp.status == 200:
            data = json.loads(body)
            body = json.dumps({k: TOKEN for k in ("token", "access_token") if k in data})
        self.exchanges.append({
            "method": method, "url": url, "authorized": "Authorization" in headers, "status": resp.status,
            "headers": {k: v for k, v in resp.headers.items() if k in KEPT_HEADERS}, "body": body,
        })
        return resp


class Replay:
    """Answers each request from the recording; an unrecorded request fails the test."""

    def __init__(self, *names: str):
        self.exchanges = [e for n in names for e in json.loads((FIXTURES / f"{n}.json").read_text())]
        self.requests: list[tuple[str, str]] = []

    def __call__(self, method, url, headers):
        self.requests.append((method, url))
        authorized = "Authorization" in headers
        if authorized:
            assert headers["Authorization"] == f"Bearer {TOKEN}", headers
        for e in self.exchanges:
            if (e["method"], e["url"], e["authorized"]) == (method, url, authorized):
                return Response(e["status"], dict(e["headers"]), e["body"].encode())
        raise AssertionError(f"unrecorded registry request: {method} {url} authorized={authorized}")


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name in sys.argv[1:] or RECORDINGS:
        recorder = Recorder()
        volumes = validate.fetch_image_volumes(RECORDINGS[name], Registry(http=recorder))
        (FIXTURES / f"{name}.json").write_text(json.dumps(recorder.exchanges, indent=1) + "\n")
        print(name, len(recorder.exchanges), "exchanges, VOLUME", volumes)


if __name__ == "__main__":
    main()
