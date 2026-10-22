# hops

Ops and diagnostics for the Host, for people and agents. `hops` is Repo Tooling: it runs on a
contributor's machine and reaches the Host over SSH. Nothing is installed on the Host.

## The core rule

**`hops` changes runtime state only** (for example restarting or stopping a container). It never
changes **declared state** on the Host. Image tags and digests change only through a PR that
doco-cd deploys after merge ([ADR 0001](../../docs/adr/0001-doco-cd-for-gitops.md)). `hops` never
runs `docker compose`.

## Install

```sh
uv tool install -e tools/hops     # from the repo root; puts `hops` on PATH
hops --help
```

Python 3.11 or later, with PyYAML. The install must be editable (`-e`): `hops` reads Compose files
through `tools/declared-containers` in the same checkout. Without installing, run
`PYTHONPATH=tools/hops python3 -m hops …`.

## Configuration

| Setting | Source | Default |
|---|---|---|
| `TRUENAS_HOST` | `.env` or environment (required) | |
| `HOPS_SSH_USER` | `.env` or environment | `homeops-agent` |
| `HOPS_SSH_KEY` | `.env` or environment | `~/.ssh/homeops_agent_ed25519` |
| `HOPS_STACK_ROOT` | environment | `tn` |

- **Repo root:** `git rev-parse --show-toplevel` of the current directory, if that checkout has a
  Stack root (`tn/`). Otherwise `hops` uses the checkout it was installed from. Names and pins come
  from that checkout's working tree. To compare the Host with `main`, run from a checkout of `main`.
- **`.env`:** the repo root's `.env`, or else the main worktree's. Agent worktrees under
  `.worktrees/` have no `.env` of their own, so they use the main one. Environment variables
  override `.env`. A missing `TRUENAS_HOST` fails with the key name and the files searched.
- **SSH:** `ssh -i $HOPS_SSH_KEY $HOPS_SSH_USER@$TRUENAS_HOST sudo -n docker …` with
  `BatchMode=yes`, so `hops` never prompts. Calls share one connection
  (`ControlMaster=auto`, `ControlPersist=10m`). The socket is under
  `${XDG_CACHE_HOME:-~/.cache}/hops/`.

## Targets

Names resolve from the repo's Compose files (`tn/<stack>/compose.yaml` and its `include:`s), not
from running containers. An unknown or ambiguous name fails before anything touches the Host.

- `stack/service` always works.
- A bare Service name works when only one Stack declares it.
- A bare Stack name: commands that take several targets (`status`) act on every Service in the
  Stack. Commands that take one Service (`inspect`, `logs`) accept it only if the Stack has
  exactly one Service.
- A name that is both a Stack and a Service, such as `nextcloud`, means the Stack for multi-target
  commands and the Service for single-Service commands. Write `nextcloud/nextcloud` to be explicit.

## Commands

Every command below is **read-only**. Each takes `--json`.

| Command | What it shows |
|---|---|
| `hops status [target…]` | For each declared Service: running or not, health, restart count, and **digest drift** (the container runs a different digest from the one this checkout pins: run from `main`, this means a Deploy did not apply). With no targets, it also lists running containers that no Stack declares. |
| `hops inspect <service>` | Image, state, start time, last exit code, OOM-killed flag, memory usage against the limit, and the health-check log |
| `hops logs <service> [--since D] [--tail N] [-f]` | `docker logs` with the options passed through. `-f` streams through `ssh -t`, until Ctrl-C. |
| `hops alerts` | Firing alerts (including silenced and inhibited ones) and active silences, from `amtool` in the alertmanager container |

`alerts` authenticates without sending the password to this machine. The Host pipes Prometheus's
Alertmanager password file (`/run/secrets/prometheus_password`) from the prometheus container
into the alertmanager container. There, `sh` hands it to `amtool` on stdin, so it never appears
in a process argument.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. `status` returns 0 even when Services are down; read the output for problems. |
| 1 | A Host call failed (SSH unreachable, docker error, no container). `logs` returns `docker logs`' own exit code. |
| 2 | Usage error before any Host call: bad arguments, unknown or ambiguous name, missing config |

Errors go to stderr as `hops: error: …`, including with `--json`.

## JSON output

`--json` prints one JSON document to stdout (`logs --json` cannot be combined with `-f`). The
fields below are stable. New fields may be added, but existing fields are not renamed or removed
without a note here. A field is `null` when the value is unknown or does not apply.

### `status --json`

```json
{
  "services": [
    {
      "stack": "nextcloud",
      "service": "mariadb",
      "tier": "critical",
      "container": "mariadb",
      "state": "running",
      "health": "healthy",
      "restart_count": 0,
      "declared_image": "mariadb:11.8.9@sha256:…",
      "running_image": "mariadb:11.8.9@sha256:…",
      "digest_drift": false
    }
  ],
  "undeclared": [
    {"container": "adhoc", "image": "alpine:3", "state": "running", "stack": null, "service": null}
  ]
}
```

- `services`: one entry for each declared Service, or each target, in Stack then Service order.
- `state`: Docker's state (`running`, `exited`, `restarting`, `paused`, `created`, `dead`), or
  `missing` when no container exists.
- `health`: `healthy`, `unhealthy`, `starting`, or `null` when there is no healthcheck or no
  container.
- `container`: the running container, or else the newest one. When the Service has none, this is
  the name Compose would give it.
- `digest_drift`: `true` when the container's digest differs from the repo's pin, `false` when
  they match, and `null` when either side has no digest.
- `undeclared`: running containers whose Compose labels match no declared Service. `stack` and
  `service` hold their Compose project and service labels, if any. This is empty when targets are
  given. It is a hint only; Stray handling is `hops strays`.

### `inspect --json`

```json
{
  "stack": "nextcloud", "service": "mariadb", "container": "mariadb",
  "image": "mariadb:11.8.9@sha256:…", "state": "running", "health": "healthy",
  "started_at": "2026-10-21T20:00:00.000000000Z", "exit_code": 0, "oom_killed": false,
  "restart_count": 0,
  "memory": {"usage_bytes": 536870912, "limit_bytes": 1610612736, "percent": 33.3},
  "health_log": [{"start": "…", "end": "…", "exit_code": 0, "output": ""}]
}
```

`memory` is `null` when the container is not running. `limit_bytes` and `percent` are `null` when
there is no memory limit.

### `logs --json`

```json
{
  "stack": "edge", "service": "traefik", "container": "traefik",
  "lines": [{"time": "2026-10-21T21:00:00.000000001Z", "stream": "stdout", "text": "…"}]
}
```

`lines` merges stdout and stderr in time order (`docker logs --timestamps`).

### `alerts --json`

```json
{
  "alerts": [
    {
      "labels": {"alertname": "ContainerDown", "stack": "nextcloud", "tier": "critical"},
      "annotations": {"summary": "…"},
      "state": "active",
      "starts_at": "…",
      "silenced_by": [],
      "inhibited_by": []
    }
  ],
  "silences": [
    {
      "id": "…", "state": "active", "matchers": ["stack=~\"plex|emby\""],
      "starts_at": "…", "ends_at": "…", "created_by": "…", "comment": "…"
    }
  ]
}
```

`state` is `active` or `suppressed` (silenced or inhibited). `matchers` use amtool's syntax.

## Adding a command

- One module in `hops/commands/` per command. It exposes `register(subparsers)`, which adds a
  parser and calls `set_defaults(func=run)`, where `run(args, ctx) -> int`. Modules are found
  automatically.
- `ctx.declared` (`hops.declared.Declared`) resolves names with `resolve(names)` or
  `resolve_one(name)`. Each `Service` has `tier`, `image` (repository, tag, digest), `container`
  and `datastore`.
- `ctx.runner` (`hops.runner.Runner`) is the only way to reach the Host. `docker(*args)`
  captures output. `pipeline(*commands)` pipes docker commands into each other on the Host.
  `attach(*args, tty=)` connects the terminal. `hops.docker` reads containers, and
  `hops.alertmanager.amtool` runs amtool.
- Raise `hops.errors.UsageError` for bad input (exit 2) and `HopsError` for failures (exit 1).

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/hops                # fake Host; runs in CI (validate workflow)
python3 -m pytest tools/hops -m live        # read-only, against the real Host
```

The `fake_host` fixture (`tests/conftest.py`) replaces the ssh process. It records each argv and
answers with canned `docker` output. The `hops` fixture runs the CLI against a fixture repo. The
`live` suite is skipped unless selected with `-m live`. It must only ever read.
