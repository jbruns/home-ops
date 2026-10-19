# dashboards

Generates the Grafana dashboards provisioned from the repo (the `home-ops` folder, read-only in
the UI). The generated JSON lives in `tn/monitoring/grafana/dashboards/`, where Grafana's
provisioning reads it; edit the generator, never the JSON. Repo Tooling: it never runs on the Host.

## Run

```sh
python3 tools/dashboards/dashboards.py           # rewrite tn/monitoring/grafana/dashboards/*.json
python3 tools/dashboards/dashboards.py --check   # exit 1 if any JSON is stale, missing or extra
```

Commit the regenerated JSON with the generator change; `validate.py monitoring` runs `--check`.
Standard library only.

## Configuration

- `--out DIR`: the dashboards folder to write or check (default `tn/monitoring/grafana/dashboards`).

Datasources are referenced by fixed uids; see Grafana in the [root README](../../README.md).

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/dashboards
```
