# validate

The repository checks CI runs on every PR (the required `validate` check) and the pre-commit
hook runs before each commit. Repo Tooling: it never runs on the Host.

| Check | What it enforces |
|---|---|
| `secrets` | Sensitive files are tracked only SOPS-encrypted as `*.sops.*`; Stack layout (`tn/<stack>/compose.yaml`, `<service>/service.yaml`); `.doco-cd.yml` keeps `auto_discovery.depth: 1` |
| `env` | Every `${VAR}` a Stack uses is in its `env.template` and `.sops.env`; `dependencies.yml` placeholders reach `dependencies-render` |
| `compose` | `docker compose config` renders every Stack and template; startup-order, logging, restart, Probe and Baseline rules; no `env_file` `format` (doco-cd supports only the default) ([ADR-0007](../../docs/adr/0007-service-baseline-via-extends.md)); volume names, external data volumes and no anonymous volumes ([ADR-0010](../../docs/adr/0010-volumes-named-for-their-stack.md); see [Volumes](#volumes)) |
| `monitoring` | Full Prometheus config and rule checks, behavioral rule suites, amtool/Loki/Alloy config checks, dashboard generation and query checks, declared-containers freshness, and Grafana provisioning boots, using the deployed images |
| `cloudflare` | Traefik's trusted Cloudflare ranges are valid and identical (see [cloudflare-ips](../cloudflare-ips/)) |
| `isolation` | `tn/doco-cd` and `tn/_baseline.yaml` changes ship in their own PR |

## Run

```sh
python3 -m pip install -r tools/validate/requirements.txt
python3 tools/validate/validate.py                      # every check
python3 tools/validate/validate.py secrets env          # just these
```

`compose` and `monitoring` need Docker; `compose` also reads image configs from their registries
(see [Volumes](#volumes)). With no arguments every check runs, including `isolation`.

## Configuration

- `--root DIR`: the repository to check (default: the one containing this file).
- `--path NAME=PATH`: move one input, relative to `--root` or absolute. Names and defaults:
  `prometheus` (`tn/monitoring/prometheus`), `alertmanager`, `loki`, `alloy` (each
  `tn/monitoring/<name>`), `blackbox` (`tn/monitoring/blackbox-exporter/blackbox.yml`),
  `dashboards` (`tools/dashboards/dashboards.py`, the generator), `dashboards-json`
  (`tn/monitoring/grafana/dashboards`, the generated JSON it checks), `grafana`
  (`tn/monitoring/grafana`), `declared-containers`
  (`tools/declared-containers/declared_containers.py`, run with `--check --root`),
  `rule-tests` (`tools/validate/rules`),
  `traefik` (`tn/edge/traefik/service.yaml`).
- `VALIDATE_BASE`: the comparison ref for `isolation`, rule-test freshness and the Grafana upgrade
  boot (default `origin/main`; CI fetches history and sets the PR base).
- `GITHUB_ACTIONS`: when set, failures print as GitHub annotations.

Exit status is 1 if any check failed.

## Volumes

`compose` reads each Stack's top-level `volumes:` as written, since the rendered model fills in a
default name:

- Every volume has an explicit `name:`.
- A volume is `external: true` (it holds data, so Compose can never remove it), or it carries the
  label `homeops.regenerable: "<what regenerates it>"`.
- The name starts with `<stack>_`. An external volume may instead carry another Stack's prefix,
  when it borrows that Stack's volume. `LEGACY_VOLUMES` in `validate.py` lists the names from
  before the one-Stack-per-app split. Remove a name once `hops volume move` renames it; the check
  fails while the list holds a name that no Stack declares. Templates are checked as Stack `app`.

Anonymous volumes are a Baseline item (`anonymous-volumes`). It applies to every Service the
Baseline applies to, including doco-cd's own Services, which cannot extend it. In practice that
is every Service. A volume with only a target (`- /data`) always fails. Every `VOLUME` path in
the image config must have a volume, bind mount or `tmpfs` at that exact path. A mount on a parent
directory does not count, because Docker still creates the anonymous volume beneath it.
`baseline.exempt.anonymous-volumes` takes a reason like the other items, and it is reported as
stale once the Service complies.

The image config is fetched anonymously from its registry (Docker Hub, GHCR, lscr.io, quay.io
and other standard registries) by the pinned digest. For an index, the `linux/amd64` image is
used. Results are cached by digest in `${XDG_CACHE_HOME:-~/.cache}/home-ops/validate/image-volumes/`;
CI restores that cache keyed on the digests in `tn/` and `templates/`. Rate limits and server
errors are retried twice. After that, a registry failure fails the check and names the image. An
image without a digest is skipped with a warning (it already fails the digest item).

## Monitoring checks

No decryption key, production credential or Host access is needed.

- `promtool check config` mounts the real rules, a rendered `dependencies.yml` with dummy
  URLs/keys at `/etc/prometheus/sd/dependencies.yml`, and dummy Compose secrets at
  `/run/secrets/<name>`. Referenced-file checks are not disabled with `--syntax-only`.
- `promtool test rules` runs every `rules/*.test.yml` with the deployed rule files.
  Every alert needs firing and nonfiring cases (except the always-firing Watchdog), and
  every firing expectation must include the `tier` and `stack` labels Alertmanager routes
  and inhibits on. A templated `tier` or `stack` (`{{ $labels.X }}`) must have `X` pinned
  non-empty in the expression (`X!=""`, a non-empty `=~`, or a `label_replace` default) so it
  never routes as the literal template. An alert with `for:` needs, in one test group, a nonfiring evaluation at
  most `for:` before a firing one, which proves the pending period. Every recording rule
  needs an assertion. Suites also cover recovery, thresholds, the other inhibiting labels
  and the recording rules used by alerts. Use
  real input metrics, not fabricated recordings, when testing an alert's recording chain.
  New or changed rules must add or update their relevant behavioral case compared with
  `VALIDATE_BASE`. Changes only to comments do not require a test edit.
- Grafana boots with the shared `tn/monitoring/grafana/provisioning/datasources.yml`,
  rendered with dummy URLs and credentials, and the real dashboard provider/JSON.
  CI checks health, provisioning logs and the actual provisioned datasource/dashboard uids.
  It reuses the database for a second boot, seeding the first boot from the base ref's
  datasource template when available, so uid/name changes must carry their migration
  (`deleteDatasources`) rather than only working on an empty database. A base predating the
  plaintext template can only exercise a clean install followed by repeat provisioning.
- Every panel target (including hidden targets and nested rows), query variable and annotation
  is checked against its datasource. Datasource variables must resolve to provisioned sources.
  Grafana variables use synthetic values and duration macros use valid fixed intervals;
  undeclared variables/unsupported formats fail rather than silently disappearing.
  Classic Prometheus variable wrappers such as `label_values` are translated before checking.
  An empty Prometheus instance checks PromQL through `/api/v1/query`; Loki checks LogQL
  through `/loki/api/v1/format_query`. These are syntax checks, not proof that metrics or
  logs exist. Failures name the dashboard, panel/variable/annotation and original query.

Disposable containers publish random ports on loopback only and are removed with their anonymous
volumes on exit; Grafana's test database lives in a temporary directory. Plugin downloads and
Grafana update/telemetry checks are disabled.

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/validate
```

The tests build small throwaway Git repositories. The `compose` test is skipped without Docker.
They never reach a registry: they replay recordings in `tests/fixtures/registry/`. To re-record
them, run `python3 tools/validate/tests/registry_fixtures.py`, which needs network access and
redacts tokens.
