"""Recorded registry v2 HTTP exchanges for hops bump tests, and the tools that record and replay them.

Re-record (needs network). Tokens are redacted, and tag lists keep only version-shaped tags (and
`latest`) to keep the files small:

    PYTHONPATH=tools/hops:tools/hops/tests python3 -m hops_registry_fixtures
"""
import json
from pathlib import Path

from hops.registry import Registry, Response, urllib_http
from hops.tags import VERSION_TAG

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "registry"
TOKEN = "anonymous-token"
KEPT_HEADERS = ("content-type", "docker-content-digest", "link", "www-authenticate")
# name -> (repository as written in Compose, tag whose digest is recorded)
RECORDINGS = {
    "dockerhub": ("traefik", "v3.7.15"),
    "ghcr": ("ghcr.io/home-operations/sonarr", "4.0.20.3012"),
    "lscr": ("lscr.io/linuxserver/qbittorrent", "5.2.4"),
}


class Recorder:
    def __init__(self):
        self.exchanges = []

    def __call__(self, method, url, headers):
        resp = urllib_http(method, url, headers)
        body = resp.body.decode()
        if "token" in url.split("?")[0].rsplit("/", 1)[-1] and resp.status == 200:
            data = json.loads(body)
            body = json.dumps({k: TOKEN for k in ("token", "access_token") if k in data})
        elif "/tags/list" in url and resp.status == 200:
            data = json.loads(body)
            data["tags"] = [t for t in data.get("tags") or [] if t == "latest" or VERSION_TAG.match(t)]
            body = json.dumps(data)
        self.exchanges.append({
            "method": method, "url": url, "authorized": "Authorization" in headers,
            "accept": headers.get("Accept"), "status": resp.status,
            "headers": {k: v for k, v in resp.headers.items() if k in KEPT_HEADERS}, "body": body,
        })
        return resp


class Replay:
    """Answers each request from the recording; an unrecorded request fails the test."""

    def __init__(self, name: str):
        self.exchanges = json.loads((FIXTURES / f"{name}.json").read_text())
        self.requests: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers):
        self.requests.append((method, url, dict(headers)))
        authorized = "Authorization" in headers
        if authorized:
            assert headers["Authorization"] == f"Bearer {TOKEN}", headers
        for e in self.exchanges:
            if (e["method"], e["url"], e["authorized"]) == (method, url, authorized):
                return Response(e["status"], dict(e["headers"]), e["body"].encode())
        raise AssertionError(f"unrecorded registry request: {method} {url} authorized={authorized}")


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, (repository, tag) in RECORDINGS.items():
        recorder = Recorder()
        Registry(http=recorder).digest(repository, tag)
        Registry(http=recorder).tags(repository)  # a fresh client, so it records its own token exchange
        (FIXTURES / f"{name}.json").write_text(json.dumps(recorder.exchanges, indent=1) + "\n")
        print(name, len(recorder.exchanges), "exchanges")


if __name__ == "__main__":
    main()
