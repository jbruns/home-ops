"""Characterization tests for validate.py: they pin what each check accepts and rejects."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import BASE_FILES, CIDRS, TOOL_DIR, commit, write
from registry_fixtures import RECORDINGS, Replay, Response

REPO_ROOT = TOOL_DIR.parents[1]
SOPS_ONLY = "sops_version=3\n"


def messages(validate) -> str:
    return "\n".join(validate.failures)


# --- pure helpers -----------------------------------------------------------------------------

@pytest.mark.parametrize("key,value", [
    ("PROXY_SUBNET", "10.99.0.0/24"),
    ("DOCKER_HOST_IPV4", "10.99.0.1"),
    ("APP_UID", "1000"),
    ("APP_GID", "1000"),
    ("WEB_PORT", "1000"),
    ("ZFS_POOL_ROOT_HDD", "/mnt/placeholder"),
    ("DOCKER_DATA_ROOT", "/mnt/placeholder"),
    ("EMAIL", "placeholder"),
])
def test_placeholder(validate, key, value):
    assert validate.placeholder(key) == value


def test_var_re_skips_escaped_dollar(validate):
    assert validate.VAR_RE.findall("a ${ONE} $${TWO} ${THREE:-x}") == ["ONE", "THREE"]


def test_env_keys_ignores_comments_blanks_and_sops_metadata(validate, tmp_path):
    f = tmp_path / "x.env"
    f.write_text("# c\n\nA=1\n B = 2\nsops_version=3\nnot a pair\n")
    assert validate.env_keys(f) == {"A", "B"}
    assert validate.env_keys(tmp_path / "missing") == set()


def test_compose_files_follows_includes(validate, repo):
    files = validate.compose_files(repo / "tn/monitoring/compose.yaml")
    assert [f.relative_to(repo).as_posix() for f in files] == [
        "tn/monitoring/compose.yaml",
        "tn/monitoring/grafana/service.yaml",
        "tn/monitoring/alloy/service.yaml",
        "tn/monitoring/loki/service.yaml",
        "tn/monitoring/alertmanager/service.yaml",
        "tn/monitoring/prometheus/service.yaml",
    ]


def test_sensitive_refs(validate, tmp_path):
    f = tmp_path / "service.yaml"
    f.write_text(
        "services:\n"
        "  a:\n"
        "    env_file: [one.sops.env, {path: two.sops.env}]\n"
        "    volumes:\n"
        "      - {type: bind, source: ./cfg.sops.yml, target: /c, x-sops: true}\n"
        "      - {type: bind, source: ./plain.yml, target: /p}\n"
        "secrets:\n"
        "  pw: {file: pw.sops.txt}\n"
    )
    assert [p.name for p in validate.sensitive_refs(f)] == ["one.sops.env", "two.sops.env", "cfg.sops.yml", "pw.sops.txt"]


BASELINE_SVC = {
    "healthcheck": {"test": ["CMD", "true"]},
    "security_opt": ["no-new-privileges:true"],
    "image": "x@sha256:abc",
    "deploy": {"resources": {"limits": {"memory": "64M"}}},
    "cap_drop": ["ALL"],
}


def test_baseline_met(validate):
    assert validate.baseline_problems("app", dict(BASELINE_SVC)) == ([], [])


def test_baseline_missing_item_without_reason(validate):
    svc = {**BASELINE_SVC, "cap_drop": []}
    problems, _ = validate.baseline_problems("app", svc)
    assert problems == ["app: misses Baseline item 'cap_drop' without a baseline.exempt.cap_drop reason"]


def test_baseline_exemption_is_reported(validate):
    svc = {**BASELINE_SVC, "image": "x:1", "labels": {"baseline.exempt.digest": "local build"}}
    assert validate.baseline_problems("app", svc) == ([], ["app: digest: local build"])


def test_baseline_stale_and_unknown_exemptions(validate):
    svc = {**BASELINE_SVC, "labels": {"baseline.exempt.digest": "x", "baseline.exempt.bogus": "y"}}
    problems, _ = validate.baseline_problems("app", svc)
    assert "app: stale Exemption baseline.exempt.digest; the Service meets it" in problems
    assert any("baseline.exempt.bogus names no Baseline item" in p for p in problems)


def test_baseline_anonymous_volumes(validate):
    assert validate.baseline_problems("app", dict(BASELINE_SVC), uncovered=[]) == ([], [])
    problems, _ = validate.baseline_problems("app", dict(BASELINE_SVC), uncovered=["/config", "/plugin"])
    assert problems == ["app: misses Baseline item 'anonymous-volumes' without a baseline.exempt.anonymous-volumes "
                        "reason (image VOLUME paths with no mount: /config, /plugin)"]


def test_baseline_anonymous_volumes_exemption(validate):
    svc = {**BASELINE_SVC, "labels": {"baseline.exempt.anonymous-volumes": "image writes a scratch dir"}}
    assert validate.baseline_problems("app", svc, uncovered=["/x"]) == ([], ["app: anonymous-volumes: image writes a scratch dir"])
    problems, _ = validate.baseline_problems("app", svc, uncovered=[])
    assert problems == ["app: stale Exemption baseline.exempt.anonymous-volumes; the Service meets it"]
    # Not checked (registry outage, reported on its own): neither stale nor missing.
    assert validate.baseline_problems("app", svc) == ([], [])


def test_baseline_rejects_declared_anonymous_volume(validate):
    svc = {**BASELINE_SVC, "volumes": [{"type": "volume", "target": "/data", "volume": {}}]}
    problems, _ = validate.baseline_problems("app", svc)
    assert problems == ["app: volume at /data has no source (an anonymous volume); name it, bind it or make it a tmpfs"]


def test_uncovered_image_volumes_need_a_mount_at_the_exact_path(validate):
    svc = {
        "volumes": [
            {"type": "volume", "source": "data", "target": "/data/"},
            {"type": "bind", "source": "/srv", "target": "/srv"},
            {"type": "tmpfs", "target": "/run"},
            {"type": "volume", "target": "/anon"},
        ],
        "tmpfs": ["/attach:uid=1000,mode=0700", "/tmp"],
    }
    image = ["/data", "/srv", "/run", "/attach", "/tmp", "/anon", "/srv/sub", "/config"]
    # A mount above a VOLUME path does not cover it: Docker still creates the anonymous volume there.
    assert validate.uncovered_volumes(svc, image) == ["/anon", "/config", "/srv/sub"]


VOLUME_STACKS = {"app", "edge", "monitoring"}


@pytest.mark.parametrize("volumes,expected", [
    ({"data": {"name": "app_data", "external": True}}, []),
    ({"cache": {"name": "app_cache", "labels": {"homeops.regenerable": "rebuilt at start"}}}, []),
    ({"cache": {"name": "app_cache", "labels": ["homeops.regenerable=rebuilt at start"]}}, []),
    ({"log": {"name": "edge_traefik_log", "external": True}}, []),
    ({"old": {"name": "media_app_config", "external": True}}, []),
    ({"data": {"external": True}}, ["app: volume data has no explicit name:"]),
    ({"data": None}, ["app: volume data has no explicit name:",
                      "app: volume data must be external: true (it holds data) or carry a homeops.regenerable label "
                      "saying what regenerates it"]),
    ({"cache": {"name": "app_cache", "labels": {"homeops.regenerable": " "}}},
     ["app: volume app_cache must be external: true (it holds data) or carry a homeops.regenerable label "
      "saying what regenerates it"]),
    ({"data": {"name": "web_data", "external": True}}, ["app: volume name web_data must start with app_ (ADR 0010)"]),
    ({"log": {"name": "edge_log", "labels": {"homeops.regenerable": "x"}}},
     ["app: volume name edge_log must start with app_ (ADR 0010)"]),
])
def test_volume_problems(validate, monkeypatch, volumes, expected):
    monkeypatch.setattr(validate, "LEGACY_VOLUMES", {"media_app_config"})
    assert validate.volume_problems("app", volumes, VOLUME_STACKS) == expected


def test_router_names_and_rules(validate):
    labels = {
        "traefik.http.routers.app-https.rule": "Host(`a`)",
        "traefik.http.routers.app-https.middlewares": "protected@file",
        "traefik.http.routers.wrong.rule": "Host(`a`)",
        "traefik.http.routers.app-client-api.rule": "Host(`a`) && Header(`X`, `y`)",
        "traefik.http.services.other.loadbalancer.server.port": "80",
    }
    problems = validate.router_problems("app", labels)
    assert problems == [
        "app: router app-client-api skips forward auth, so it needs strip-idp-headers",
        "app: Client Path app-client-api needs crowdsec-bouncer and a rate limit",
        "app: router 'wrong' must be app-http|-https|-health|-client-<protocol>",
        "app: a Protected Service needs a probe.idp_url label",
        "app: a Protected Service needs a app-health router for its Probe",
        "app: Traefik service 'other' must be named 'app' (Http5xxRatio maps it to the container)",
    ]


def test_router_health_rules(validate):
    labels = {
        "traefik.http.routers.app-health.rule": "Host(`a`) && PathPrefix(`/h`)",
        "traefik.http.routers.app-health.middlewares": "idp-forward-auth",
        "traefik.http.routers.app-http.rule": "Host(`a`) && Header(`X`, `y`)",
    }
    assert validate.router_problems("app", labels) == [
        "app: router app-health must match one exact Path()",
        "app: router app-health must not use forward auth",
        "app: router app-health skips forward auth, so it needs strip-idp-headers",
        "app: router app-http matches on a header; only Client Paths may",
        "app: a Protected Service needs a probe.idp_url label",
    ]


def test_routers_skipping_forward_auth_strip_idp_headers(validate):
    labels = {
        "traefik.http.routers.app-https.middlewares": "protected",
        "traefik.http.routers.app-health.rule": "Host(`a`) && Path(`/health`)",
        "traefik.http.routers.app-health.middlewares": "crowdsec-bouncer",
        "traefik.http.routers.app-client-opds.rule": "Host(`a`) && PathPrefix(`/opds`)",
        "traefik.http.routers.app-client-opds.middlewares": "crowdsec-bouncer,strip-idp-headers@docker,client-ratelimit",
        "probe.idp_url": "https://a/",
    }
    assert validate.router_problems("app", labels) == [
        "app: router app-health skips forward auth, so it needs strip-idp-headers",
    ]


def test_strip_idp_headers_covers_forward_auth_headers(validate):
    mw = "traefik.http.middlewares"
    labels = {
        f"{mw}.idp-forward-auth.forwardAuth.authResponseHeaders": "X-a-user,X-a-groups,X-a-jwt",
        f"{mw}.strip-idp-headers.headers.customRequestHeaders.X-a-user": "",
        f"{mw}.strip-idp-headers.headers.customRequestHeaders.X-a-groups": "x",
    }
    assert validate.router_problems("edge", labels) == [
        "edge: strip-idp-headers must empty every idp-forward-auth header; missing X-a-groups, X-a-jwt",
    ]


def test_probe_problems(validate):
    labels = {"traefik.http.routers.app-https.rule": "Host(`a`)", "probe.module": "nope"}
    assert validate.probe_problems("app", labels, {"http_2xx"}) == [
        "app: has an https router but no probe.url label",
        "app: probe.module 'nope' is not a blackbox.yml module",
    ]
    ok = {"traefik.http.routers.app-https.rule": "Host(`a`)", "probe.url": "https://a"}
    assert validate.probe_problems("app", ok, {"http_2xx"}) == []


# --- image VOLUME paths (recorded registry exchanges) -------------------------------------------

@pytest.mark.parametrize("name,volumes", [
    ("dockerhub", ["/attach", "/config", "/plugin"]),  # what apprise mounts anonymously on the Host
    ("dockerhub-single", []),
    ("ghcr", ["/config"]),
    ("lscr", ["/config"]),
    ("quay", []),
])
def test_image_volumes_from_each_registry(validate, name, volumes):
    http = Replay(name)
    assert validate.image_volumes(RECORDINGS[name], validate.Registry(http)) == volumes


def test_image_volumes_reads_the_linux_amd64_image_of_an_index(validate):
    http = Replay("dockerhub")
    validate.image_volumes(RECORDINGS["dockerhub"], validate.Registry(http))
    # apprise's index lists amd64, arm64 and two attestation manifests; amd64 is fc45ea2c...
    manifests = [url.rsplit("/", 1)[-1] for method, url in http.requests if "/manifests/" in url]
    assert manifests[-1].startswith("sha256:fc45ea2c888f990c")


def test_image_volumes_are_cached_by_digest(validate):
    image = RECORDINGS["ghcr"]
    validate.image_volumes(image, validate.Registry(Replay("ghcr")))
    assert validate.image_volumes(image, validate.Registry(validate.REGISTRY_HTTP)) == ["/config"]


def test_image_without_an_amd64_image_is_an_error(validate):
    index = b'{"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[{"digest":"sha256:1","platform":{"os":"linux","architecture":"arm64"}}]}'

    def http(method, url, headers):
        return Response(200, {}, index)

    with pytest.raises(validate.HopsError, match="has no linux/amd64 image"):
        validate.image_volumes("registry.example/app:1@sha256:abc", validate.Registry(http))


# --- secrets ----------------------------------------------------------------------------------

def test_secrets_ok(validate, capsys):
    validate.check_secrets()
    assert validate.failures == []
    assert "secrets ok" in capsys.readouterr().out


@pytest.mark.parametrize("files,expected", [
    ({"tn/app/web/secret.sops.env": "TOKEN=plain\n"}, "file is named *.sops.* but is not SOPS-encrypted"),
    ({"tn/app/web/secret.env": "TOKEN=x\n"}, "plaintext twin of a SOPS file is tracked: tn/app/web/secret.env"),
    ({"tn/app/extra.env": "X=1\n"}, "plaintext .env file is tracked; encrypt it as *.sops.env"),
    ({"tn/app/docker-compose.yml": "services: {}\n"}, "rename to service.yaml (one Service per file) or compose.yaml"),
    ({"tn/lonely/service.yaml": "services: {}\n"}, "doco-cd never discovers this"),
    ({"tn/app/orphan/service.yaml": "services: {}\n"}, "no Stack's include: lists this service.yaml"),
    ({"tn/app/compose.yaml": "include: [web/other.yaml]\n", "tn/app/web/other.yaml": "services: {}\n"},
     "app: included file tn/app/web/other.yaml must be tn/app/<service>/service.yaml"),
    ({".doco-cd.yml": "auto_discovery:\n  depth: 0\n"}, "auto_discovery.depth must stay 1 (found 0)"),
    ({"tn/app/web/service.yaml": "services:\n  web:\n    env_file: missing.sops.env\n"},
     "referenced file tn/app/web/missing.sops.env is not tracked"),
    ({"tn/app/web/service.yaml": "services:\n  web:\n    env_file: plain.cfg\n", "tn/app/web/plain.cfg": "x\n"},
     "sensitive file tn/app/web/plain.cfg must be SOPS-encrypted and named *.sops.*"),
])
def test_secrets_rejects(validate, repo, files, expected):
    write(repo, files)
    commit(repo)
    validate.check_secrets()
    assert expected in messages(validate)


def test_secrets_requires_tracked_stack_env(validate, repo):
    (repo / "tn/app/.sops.env").unlink()
    commit(repo)
    validate.check_secrets()
    assert "app: Stack has no tracked .sops.env" in messages(validate)


def test_secrets_ignores_host_paths_outside_repo(validate, repo):
    write(repo, {"tn/app/web/service.yaml": "services:\n  web:\n    env_file: /mnt/age.key\n"})
    commit(repo)
    validate.check_secrets()
    assert validate.failures == []


# --- env --------------------------------------------------------------------------------------

def test_env_ok_warns_on_unused_template_keys(validate, repo, capsys):
    write(repo, {"tn/app/env.template": "GREETING=\nUNUSED=\n"})
    validate.check_env()
    out = capsys.readouterr().out
    assert validate.failures == []
    assert "WARN: app: env.template keys no Compose file uses: UNUSED" in out
    assert "env checked: app" in out


def test_env_missing_keys(validate, repo):
    write(repo, {"tn/app/env.template": "", "tn/app/.sops.env": SOPS_ONLY})
    validate.check_env()
    assert messages(validate).splitlines() == [
        "app: variables used but missing from env.template: GREETING",
        "app: variables used but missing from .sops.env (deploy would get blanks): GREETING",
    ]



def test_env_dependencies_placeholders(validate, repo):
    write(repo, {"tn/monitoring/prometheus/dependencies.yml": "- ['${DEP_HOST}', '${OTHER}']\n"})
    validate.check_env()
    assert messages(validate) == "dependencies.yml placeholders not passed to dependencies-render: OTHER"


# --- isolation --------------------------------------------------------------------------------

def isolation(validate, repo, monkeypatch, files):
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    write(repo, files)
    commit(repo)
    monkeypatch.setenv("VALIDATE_BASE", base)
    validate.check_isolation()
    return messages(validate)


def test_isolation_ok(validate, repo, monkeypatch):
    assert isolation(validate, repo, monkeypatch, {"tn/app/web/note.txt": "x", "tn/edge/x.yml": "x"}) == ""


def test_isolation_doco_cd_alone(validate, repo, monkeypatch):
    out = isolation(validate, repo, monkeypatch, {"tn/doco-cd/x.yml": "x", "tn/app/x.yml": "x"})
    assert out == "isolation: tn/doco-cd changes must ship in their own PR (also changed: app)"


def test_isolation_baseline_alone(validate, repo, monkeypatch):
    out = isolation(validate, repo, monkeypatch, {"tn/_baseline.yaml": "x", "tn/app/x.yml": "x"})
    assert out == "isolation: tn/_baseline.yaml changes must ship in their own PR (also changed: app)"


def test_isolation_bad_base(validate, monkeypatch):
    monkeypatch.setenv("VALIDATE_BASE", "no-such-ref")
    validate.check_isolation()
    assert messages(validate).startswith("isolation: cannot diff against no-such-ref")


# --- cloudflare -------------------------------------------------------------------------------

def test_cloudflare(validate, repo, capsys):
    validate.check_cloudflare()
    assert validate.failures == [] and "cloudflare ok" in capsys.readouterr().out

    write(repo, {"tn/edge/traefik/service.yaml": f"forwardedHeadersTrustedIPs: {CIDRS}\n"})
    validate.check_cloudflare()
    assert messages(validate) == "expected 3 Cloudflare range lists, found 1"


# --- monitoring -------------------------------------------------------------------------------

def test_monitoring_checks_referenced_files_with_dummy_dependencies_and_secrets(validate, repo, runtime):
    write(repo, {
        "tn/monitoring/prometheus/service.yaml": """
            services:
              prometheus:
                image: prom/prometheus:v1
            secrets:
              prometheus_password:
                file: ./prometheus_password.sops.txt
        """,
    })
    validate.check_monitoring()
    assert not validate.failures
    config = next(r for r in runtime.runs if r.args[1:3] == ["check", "config"])
    assert "--syntax-only" not in config.args
    assert "/etc/prometheus/alerting-rules.yml" in config.mounts
    assert "${DEP_HOST}" not in config.mounts["/etc/prometheus/sd/dependencies.yml"]
    assert config.mounts["/run/secrets/prometheus_password"].strip()


def test_monitoring_runs_each_tool_with_the_deployed_image(validate, runtime):
    validate.check_monitoring()
    assert validate.failures == []
    config = runtime.runs[0]
    assert (config.image, config.args) == ("prom/prometheus:v1",
                                           ["promtool", "check", "config", "/etc/prometheus/prometheus.yml"])
    assert set(config.mounts) == {
        "/etc/prometheus/prometheus.yml", "/etc/prometheus/alerting-rules.yml",
        "/etc/prometheus/sd/dependencies.yml",
    }
    assert [(r.image, r.args[0]) for r in runtime.runs] == [
        ("prom/prometheus:v1", "promtool"),
        ("prom/prometheus:v1", "promtool"),
        ("prom/alertmanager:v2", "amtool"),
        ("grafana/loki:3", "/usr/bin/loki"),
        ("grafana/alloy:1", "/bin/alloy"),
    ]


def test_monitoring_skips_untracked_alertmanager_config_and_reports_stale_dashboards(validate, repo, capsys, runtime):
    subprocess.run(["git", "rm", "-q", "--cached", "tn/monitoring/alertmanager/config.yml"], cwd=repo, check=True)
    write(repo, {"tools/dashboards/dashboards.py": "print('overview.json: stale'); raise SystemExit(1)\n"})
    validate.check_monitoring()
    assert "amtool" not in [r.args[0] for r in runtime.runs]
    assert "alertmanager config is encrypted-only" in capsys.readouterr().out
    assert messages(validate) == "overview.json: stale"


# --- docker-backed checks ---------------------------------------------------------------------

def docker_compose_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


APPRISE = RECORDINGS["dockerhub"]  # VOLUME /attach /config /plugin


WEB = f"""
    services:
      web:
        extends: {{file: ../../_baseline.yaml, service: baseline}}
        image: {APPRISE}
        healthcheck: {{test: ["CMD", "true"]}}
        deploy: {{resources: {{limits: {{memory: 64M}}}}}}
"""


def compose_fixture(repo, service_yaml: str) -> None:
    shutil.copy(REPO_ROOT / "tn/_baseline.yaml", repo / "tn/_baseline.yaml")
    write(repo, {
        "tn/monitoring/blackbox-exporter/blackbox.yml": "modules:\n  http_2xx: {}\n",
        "tn/app/web/service.yaml": service_yaml,
    })
    commit(repo)


@pytest.mark.skipif(not docker_compose_available(), reason="needs docker compose")
def test_compose_reports_every_rule(validate, repo, capsys, monkeypatch):
    monkeypatch.setattr(validate, "REGISTRY_HTTP", Replay("dockerhub"))
    monkeypatch.setattr(validate, "LEGACY_VOLUMES", {"media_web_config", "media_gone"})
    compose_fixture(repo, WEB + """
        env_file: [{path: secret.sops.env, format: raw}]
        labels:
          startup-order: "2"
          traefik.http.routers.web-https.rule: Host(`a`)
          greeting: ${GREETING}
        tmpfs: [/attach]
        volumes:
          - config:/config
          - legacy:/legacy
          - /scratch
      bare:
        image: nginx:1
    volumes:
      config:
        name: app_config
      legacy:
        name: media_web_config
        external: true
    """)
    validate.check_compose()
    out = messages(validate)
    assert "app: services without a valid startup-order label (0-3, ADR-0003): bare" in out
    assert "app: web: has an https router but no probe.url label" in out
    assert "app: services without json-file logging at max-size 10m, max-file 3: bare" in out
    assert "app: services without restart: unless-stopped: bare" in out
    assert "app: env_file format is unsupported by doco-cd (use the default and single-quote values containing $): web" in out
    assert "app: Baseline (ADR 0007): bare: must extend tn/_baseline.yaml" in out
    assert ("web: misses Baseline item 'anonymous-volumes' without a baseline.exempt.anonymous-volumes reason "
            "(image VOLUME paths with no mount: /plugin)") in out
    assert "web: volume at /scratch has no source (an anonymous volume)" in out
    assert "app: volumes (ADR 0010): app: volume app_config must be external: true" in out
    assert "volumes (ADR 0010): legacy name media_gone is no longer declared; remove it from LEGACY_VOLUMES" in out
    assert "media_web_config" not in out
    assert "compose ok: monitoring" not in out
    assert "Baseline: " in capsys.readouterr().out


@pytest.mark.skipif(not docker_compose_available(), reason="needs docker compose")
def test_a_stack_that_fails_to_render_still_declares_its_legacy_volumes(validate, repo, monkeypatch):
    monkeypatch.setattr(validate, "LEGACY_VOLUMES", {"media_web_config"})
    compose_fixture(repo, WEB + """
        depends_on: [missing]
    volumes:
      legacy:
        name: media_web_config
        external: true
    """)
    validate.check_compose()
    assert "docker compose config failed" in messages(validate)
    assert "no longer declared" not in messages(validate)


@pytest.mark.skipif(not docker_compose_available(), reason="needs docker compose")
def test_compose_fails_clearly_when_the_registry_is_down(validate, repo, monkeypatch):
    def down(method, url, headers):
        return validate.urllib_http(method, "http://127.0.0.1:9/unreachable", headers)

    monkeypatch.setattr(validate, "REGISTRY_HTTP", down)
    monkeypatch.setattr(validate, "LEGACY_VOLUMES", set())
    compose_fixture(repo, WEB + """
        labels: {startup-order: "2", greeting: "${GREETING}"}
    """)
    validate.check_compose()
    assert messages(validate).startswith(
        f"app: web: cannot read the image config of {APPRISE} for its VOLUME paths (anonymous volumes, ADR 0007): "
        "registry request failed: GET http://127.0.0.1:9/unreachable")
    assert "anonymous-volumes" not in messages(validate).split("ADR 0007): ", 1)[1]


def test_registry_requests_retry_rate_limits_and_server_errors(validate):
    replies = [Response(429, {"retry-after": "120"}), Response(503), Response(200, body=b"ok")]
    sent, slept = [], []

    def http(method, url, headers):
        sent.append(url)
        return replies[len(sent) - 1]

    assert validate.patient(http, sleep=slept.append)("GET", "u", {}).body == b"ok"
    assert slept == [30, 8]
    replies[:] = [Response(503)] * 3
    sent.clear()
    assert validate.patient(http, sleep=slept.append)("GET", "u", {}).status == 503
    assert len(sent) == 3


# --- the real repository ----------------------------------------------------------------------

def test_cli_passes_on_this_repository():
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
    r = subprocess.run([sys.executable, str(TOOL_DIR / "validate.py"), "secrets", "env", "cloudflare"],
                       cwd=REPO_ROOT, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "secrets ok" in r.stdout and "cloudflare ok" in r.stdout


def test_cli_rejects_unknown_check():
    r = subprocess.run([sys.executable, str(TOOL_DIR / "validate.py"), "nope"], capture_output=True, text=True)
    assert r.returncode == 1 and "unknown check 'nope'" in r.stderr


def test_github_annotations(validate, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    validate.fail("a\nb", validate.ROOT / "tn/x.yml")
    validate.warn("w")
    assert capsys.readouterr().out == "::error file=tn/x.yml::a%0Ab\n::warning::w\n"


# --- configuration ----------------------------------------------------------------------------

def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
    return subprocess.run([sys.executable, str(TOOL_DIR / "validate.py"), *args], capture_output=True, text=True, env=env)


def test_cli_root_and_path_override(repo):
    write(repo, {"elsewhere/traefik.yaml": "forwardedHeadersTrustedIPs: 1.1.1.0/24\n"})
    assert run_cli("--root", str(repo), "cloudflare").returncode == 0
    r = run_cli("--root", str(repo), "--path", "traefik=elsewhere/traefik.yaml", "cloudflare")
    assert r.returncode == 1
    assert "ERROR: expected 3 Cloudflare range lists, found 1" in r.stdout


@pytest.mark.parametrize("arg", ["nope=x", "traefik"])
def test_cli_rejects_bad_path_override(arg):
    r = run_cli("--path", arg, "cloudflare")
    assert r.returncode == 2 and "--path takes NAME=PATH" in r.stderr


def test_monitoring_paths_are_configurable(validate, repo, runtime):
    write(repo, {
        "mon/prom/service.yaml": "services:\n  prometheus:\n    image: prom/prometheus:v9\n",
        "mon/prom/prometheus.yml": "",
        "mon/prom/dependencies.yml": "[]\n",
        "mon/prom/x-rules.yml": "",
    })
    commit(repo)
    validate.configure(repo, {"prometheus": "mon/prom"})
    validate.check_monitoring()
    assert (runtime.runs[0].image, set(runtime.runs[0].mounts)) == ("prom/prometheus:v9", {
        "/etc/prometheus/prometheus.yml", "/etc/prometheus/x-rules.yml",
        "/etc/prometheus/sd/dependencies.yml",
    })


def test_monitoring_checks_dashboards_json_under_root(validate, repo, runtime):
    write(repo, {"tools/dashboards/dashboards.py": "import sys; print(*sys.argv[1:]); raise SystemExit(1)\n"})
    validate.configure(repo, {"dashboards-json": "json"})
    write(repo, {"json/overview.json": '{"uid":"overview","title":"Overview","panels":[]}\n'})
    validate.check_monitoring()
    assert messages(validate) == f"--check --out {repo / 'json'}"


def test_monitoring_reports_stale_declared_containers_under_root(validate, repo, runtime):
    write(repo, {"tools/declared-containers/declared_containers.py":
                 "import sys; print('stale', *sys.argv[1:]); raise SystemExit(1)\n"})
    validate.check_monitoring()
    assert messages(validate) == f"stale --check --root {repo}"


def test_configure_rejects_unknown_path(validate, repo):
    with pytest.raises(KeyError):
        validate.configure(repo, {"nope": "x"})


def test_template_volumes_are_named_for_stack_app(validate):
    # Templates are not Stacks yet, so "app" is not among the Stack names.
    assert validate.volume_problems("app", {"app_config": {"name": "app_config", "external": True}}, {"edge"}) == []
