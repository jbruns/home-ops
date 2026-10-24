"""Characterization tests for cloudflare_ips.py."""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import cloudflare_ips  # noqa: E402

OLD = "173.245.48.0/20,2400:cb00::/32"


def traefik(*lists: str) -> str:
    keys = ["--entrypoints.https.forwardedHeaders.trustedIPs=", "--entrypoints.http.forwardedHeaders.trustedIPs=",
            "forwardedHeadersTrustedIPs: "]
    return "".join(f"      - {k}{v}\n" for k, v in zip(keys, lists))


@pytest.fixture
def traefik_file(tmp_path):
    f = tmp_path / "service.yaml"
    f.write_text("# keep me\n" + traefik(OLD, OLD, OLD))
    return f


def test_extract():
    assert cloudflare_ips.extract(traefik(OLD, "1.1.1.0/24")) == [OLD.split(","), ["1.1.1.0/24"]]


def test_problems_ok():
    assert cloudflare_ips.problems(traefik(OLD, OLD, OLD)) == []


@pytest.mark.parametrize("text,expected", [
    (traefik(OLD, OLD), ["expected 3 Cloudflare range lists, found 2"]),
    (traefik("1.2.3.4/33", "1.2.3.4/33", "1.2.3.4/33"), ["invalid CIDR '1.2.3.4/33'"]),
    (traefik(OLD, OLD, "173.245.48.0/20"), ["the Cloudflare range lists differ; keep all three identical"]),
])
def test_problems(text, expected):
    assert cloudflare_ips.problems(text) == expected


def test_problems_ignores_order():
    reordered = ",".join(reversed(OLD.split(",")))
    assert cloudflare_ips.problems(traefik(OLD, reordered, OLD)) == []


def test_update_unchanged(traefik_file, monkeypatch, capsys):
    before = traefik_file.read_text()
    monkeypatch.setattr(cloudflare_ips, "fetch", lambda: list(reversed(OLD.split(","))))
    assert cloudflare_ips.update(traefik_file) is False
    assert traefik_file.read_text() == before
    assert capsys.readouterr().out == "Cloudflare ranges unchanged\n"


def test_update_rewrites_every_list(traefik_file, monkeypatch, capsys):
    new = ["104.16.0.0/13", "2606:4700::/32"]
    monkeypatch.setattr(cloudflare_ips, "fetch", lambda: new)
    assert cloudflare_ips.update(traefik_file) is True
    assert traefik_file.read_text() == "# keep me\n" + traefik(*[",".join(new)] * 3)
    assert capsys.readouterr().out == "Cloudflare ranges updated: 2 CIDRs\n"


def test_update_refuses_inconsistent_file(traefik_file, monkeypatch):
    traefik_file.write_text(traefik(OLD))
    monkeypatch.setattr(cloudflare_ips, "fetch", lambda: pytest.fail("must not fetch"))
    with pytest.raises(SystemExit, match="expected 3 Cloudflare range lists, found 1"):
        cloudflare_ips.update(traefik_file)


def fake_api(monkeypatch, body: dict) -> None:
    monkeypatch.setattr(cloudflare_ips.urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(json.dumps(body).encode()))


def test_fetch(monkeypatch):
    fake_api(monkeypatch, {"success": True, "result": {"ipv4_cidrs": ["1.1.1.0/24"], "ipv6_cidrs": ["2400:cb00::/32"]}})
    assert cloudflare_ips.fetch() == ["1.1.1.0/24", "2400:cb00::/32"]


@pytest.mark.parametrize("body", [
    {"success": False, "result": {"ipv4_cidrs": ["1.1.1.0/24"]}},
    {"success": True, "result": {}},
])
def test_fetch_rejects_bad_response(monkeypatch, body):
    fake_api(monkeypatch, body)
    with pytest.raises(SystemExit, match="unexpected response"):
        cloudflare_ips.fetch()


def test_fetch_rejects_invalid_cidr(monkeypatch):
    fake_api(monkeypatch, {"success": True, "result": {"ipv4_cidrs": ["nope"]}})
    with pytest.raises(ValueError):
        cloudflare_ips.fetch()


def test_default_file_is_traefik():
    assert cloudflare_ips.TRAEFIK == TOOL_DIR.parents[1] / "tn/edge/traefik/service.yaml"


def test_cli_requires_update_flag():
    r = subprocess.run([sys.executable, str(TOOL_DIR / "cloudflare_ips.py")], capture_output=True, text=True)
    assert r.returncode == 1 and "usage: cloudflare_ips.py" in r.stderr


def test_cli_update_honours_file(traefik_file, monkeypatch, capsys):
    monkeypatch.setattr(cloudflare_ips, "fetch", lambda: ["104.16.0.0/13"])
    cloudflare_ips.main(["--update", "--file", str(traefik_file)])
    assert cloudflare_ips.extract(traefik_file.read_text()) == [["104.16.0.0/13"]] * 3
