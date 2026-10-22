# declared-containers

Generates `tn/monitoring/node-exporter/declared_containers.prom`, the desired state ContainerDown
compares with cAdvisor. Every Service in `tn/<stack>/compose.yaml` and its `include:` files becomes a
`homeops_container_declared{stack,service,name,tier} 1` line. node-exporter serves the file through its
textfile collector. Repo Tooling: it never runs on the Host.

- `stack` is the folder name, matching `com.docker.compose.project`.
- `name` is `container_name`, or Compose's `<stack>-<service>-1` when unset. That is a guess (the
  number can grow, as doco-cd's self-updates show), so ContainerDown matches on `stack` and `service`.
- `tier` is the Service's `tier` label. A Service without one, or a variable `container_name` or
  `tier`, is an error.

## Run

```sh
python3 tools/declared-containers/declared_containers.py           # rewrite the .prom file
python3 tools/declared-containers/declared_containers.py --check   # exit 1 if it is stale
```

Rerun it and commit the file whenever a Service is added, removed or renamed; `validate.py
monitoring` runs `--check`. doco-cd recreates node-exporter when the file changes. Requires PyYAML
(`tools/validate/requirements.txt`).

## Configuration

- `--root DIR`: the repository to read (default: the one containing this file).
- `--out FILE`: the file to write or check, relative to `--root` (default
  `tn/monitoring/node-exporter/declared_containers.prom`).

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/declared-containers
```
