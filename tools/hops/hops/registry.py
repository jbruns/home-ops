"""A minimal, stdlib-only client for the registry v2 API: list an image's tags and resolve a tag's
digest, anonymously, as Renovate does.

Works with Docker Hub (implicit `docker.io` and `library/`), GHCR, lscr.io (which hands out GHCR
tokens) and any registry that uses the standard Bearer token challenge.
"""
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from hops.errors import HopsError

DOCKER_HUB = "registry-1.docker.io"
# What Renovate (docker:pinDigests) asks for: with the index types listed, a multi-arch tag
# answers with its index (manifest list) digest, a single-arch tag with its manifest digest.
MANIFEST_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])
PAGE_SIZE = 1000
TIMEOUT = 30


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""


def urllib_http(method: str, url: str, headers: dict[str, str]) -> Response:
    """The real transport: one HTTP request; error statuses are returned, not raised."""
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HopsError(f"registry request failed: {method} {url}: {getattr(exc, 'reason', exc)}") from exc


def locate(repository: str) -> tuple[str, str]:
    """(registry host, repository path) for an image repository as written in Compose."""
    first, _, rest = repository.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        host, path = first, rest
    else:
        host, path = "docker.io", repository
    if host in ("docker.io", "index.docker.io"):
        host = DOCKER_HUB
        if "/" not in path:
            path = f"library/{path}"
    return host, path


def _challenge(header: str) -> dict[str, str] | None:
    """The parameters of a `Bearer realm=...,service=...,scope=...` challenge."""
    scheme, _, params = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return dict(re.findall(r'(\w+)="([^"]*)"', params))


class Registry:
    def __init__(self, http=urllib_http):
        self.http = http
        self._tokens: dict[str, str] = {}  # by registry host and repository path

    def tags(self, repository: str) -> list[str]:
        """Every tag of the repository, following pagination."""
        host, path = locate(repository)
        url = f"https://{host}/v2/{path}/tags/list?n={PAGE_SIZE}"
        tags: list[str] = []
        while url:
            resp = self._get(host, path, "GET", url, {})
            self._check(resp, repository, "tags")
            tags += json.loads(resp.body or b"{}").get("tags") or []
            url = _next_link(resp.headers.get("link"), url)
        return tags

    def digest(self, repository: str, tag: str) -> str:
        """The digest Renovate pins for repository:tag: the index's for a multi-arch image."""
        host, path = locate(repository)
        url = f"https://{host}/v2/{path}/manifests/{urllib.parse.quote(tag, safe='')}"
        headers = {"Accept": MANIFEST_ACCEPT}
        resp = self._get(host, path, "HEAD", url, headers)
        self._check(resp, f"{repository}:{tag}", "manifest")
        if digest := resp.headers.get("docker-content-digest"):
            return digest
        resp = self._get(host, path, "GET", url, headers)
        self._check(resp, f"{repository}:{tag}", "manifest")
        return resp.headers.get("docker-content-digest") or "sha256:" + hashlib.sha256(resp.body).hexdigest()

    def _get(self, host: str, path: str, method: str, url: str, headers: dict[str, str]) -> Response:
        key = f"{host}/{path}"
        if key in self._tokens:
            headers = {**headers, "Authorization": f"Bearer {self._tokens[key]}"}
        resp = self.http(method, url, headers)
        if resp.status != 401 or "Authorization" in headers:
            return resp
        params = _challenge(resp.headers.get("www-authenticate", ""))
        if not params or "realm" not in params:
            raise HopsError(f"{host} wants credentials hops does not have (anonymous pulls only)")
        self._tokens[key] = self._token(params)
        return self._get(host, path, method, url, headers)

    def _token(self, params: dict[str, str]) -> str:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if k in ("service", "scope")})
        url = params["realm"] + ("?" + query if query else "")
        resp = self.http("GET", url, {})
        if resp.status != 200:
            raise HopsError(f"anonymous token request failed: GET {url}: HTTP {resp.status}")
        data = json.loads(resp.body)
        token = data.get("token") or data.get("access_token")
        if not token:
            raise HopsError(f"anonymous token request returned no token: GET {url}")
        return token

    @staticmethod
    def _check(resp: Response, what: str, kind: str) -> None:
        if resp.status == 404:
            raise HopsError(f"{what}: no such {kind} in the registry")
        if resp.status != 200:
            raise HopsError(f"{what}: registry answered HTTP {resp.status} for the {kind}")


def _next_link(link: str | None, current: str) -> str | None:
    """The `rel="next"` URL of a Link header, resolved against the current URL."""
    m = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', link or "")
    return urllib.parse.urljoin(current, m.group(1)) if m else None
