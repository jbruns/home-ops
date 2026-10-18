# validate

The repository checks CI runs on every PR (the required `validate` check) and the pre-commit
hook runs before each commit. Repo Tooling: it never runs on the Host.

| Check | What it enforces |
|---|---|
| `secrets` | Sensitive files are tracked only SOPS-encrypted as `*.sops.*`; Stack layout (`tn/<stack>/compose.yaml`, `<service>/service.yaml`); `.doco-cd.yml` keeps `auto_discovery.depth: 1` |
| `env` | Every `${VAR}` a Stack uses is in its `env.template` and `.sops.env`; `dependencies.yml` placeholders reach `dependencies-render` |
| `compose` | `docker compose config` renders every Stack and template; startup-order, logging, restart, Probe and Baseline rules ([ADR-0007](../../docs/adr/0007-service-baseline-via-extends.md)) |
| `monitoring` | promtool, amtool, loki and alloy validate their configs using the images the Stacks deploy; Grafana dashboards match [their generator](../dashboards/) |
| `cloudflare` | Traefik's trusted Cloudflare ranges are valid and identical (see [cloudflare-ips](../cloudflare-ips/)) |
| `isolation` | `tn/doco-cd` and `tn/_baseline.yaml` changes ship in their own PR |

## Run

```sh
python3 -m pip install -r tools/validate/requirements.txt
python3 tools/validate/validate.py                      # every check
python3 tools/validate/validate.py secrets env          # just these
```

`compose` and `monitoring` need Docker. With no arguments every check runs, including `isolation`.

## Configuration

- `--root DIR`: the repository to check (default: the one containing this file).
- `--path NAME=PATH`: move one input, relative to `--root` or absolute. Names and defaults:
  `prometheus` (`tn/monitoring/prometheus`), `alertmanager`, `loki`, `alloy` (each
  `tn/monitoring/<name>`), `blackbox` (`tn/monitoring/blackbox-exporter/blackbox.yml`),
  `dashboards` (`tools/dashboards/dashboards.py`, the generator), `dashboards-json`
  (`tn/monitoring/grafana/dashboards`, the generated JSON it checks), `traefik` (`tn/edge/traefik/service.yaml`).
- `VALIDATE_BASE`: the ref `isolation` diffs against (default `origin/main`; CI sets the PR base).
- `GITHUB_ACTIONS`: when set, failures print as GitHub annotations.

Exit status is 1 if any check failed.

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/validate
```

The tests build small throwaway Git repositories. The `compose` test is skipped without Docker.
