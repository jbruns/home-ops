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

Every command in this table is **read-only**. Each takes `--json`.

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

### exec and db

| Command | What it does | Guardrail class |
|---|---|---|
| `hops exec <service> [--] cmd…` | `docker exec` the command in the Service's container; stdout and stderr stream to this terminal; returns the command's exit code | none |
| `hops exec <service>` | An interactive shell (bash if the image has one, else sh) via `ssh -t` and `docker exec -it` | Disruptive: needs a TTY |
| `hops db <datastore>` | The Datastore's own client, interactive: `mariadb` as root, `valkey-cli`, or `psql` | Disruptive: needs a TTY |
| `hops db <datastore> -e "<statement>"` | One statement, non-interactive (for agents); prints the client's output and returns its exit code | Read-only, or `--write` |

- **Interactive modes refuse without a TTY on stdin** (exit 2), so an agent cannot open a session
  that waits for input. Agents use `exec` with a command, or `db -e`.
- **`db` targets only Datastores** (GLOSSARY): Services whose image is MariaDB, Valkey or Postgres
  (or MySQL, Redis). Any other Service fails with exit 2 before the Host is touched.
- **Credentials stay in the container.** `hops` sends `docker exec … sh -c '<script>'`. That shell,
  in the container, reads the credential and hands it to the client in an environment variable,
  so it is never in an argument on this machine or in `ps` on the Host:

  | Engine | Credential | Client |
  |---|---|---|
  | MariaDB | `MARIADB_ROOT_PASSWORD_FILE` (or `MARIADB_ROOT_PASSWORD`, and the `MYSQL_` forms) | `mariadb --user=root`, password in `MYSQL_PWD` |
  | Valkey | `requirepass` in the `.conf` file named in the Service's `command` (else `VALKEY_PASSWORD`/`REDIS_PASSWORD`) | `valkey-cli`, password in `REDISCLI_AUTH` |
  | Postgres | `POSTGRES_PASSWORD_FILE` or `POSTGRES_PASSWORD`; user `POSTGRES_USER` (default `postgres`), database `POSTGRES_DB` | `psql -X`, password in `PGPASSWORD` |

  An SQL `-e` statement goes to the client on stdin. A Valkey `-e` runs each line as its own
  `valkey-cli -e <args…>` (arguments split as a shell would, so quote with `'…'` or `"…"`), in
  order, and stops at the first command that fails with its exit code; the REPL would exit 0
  after an error. What the client prints still comes back: a query that selects a secret
  (Valkey `CONFIG GET requirepass`, `mysql.user`) shows it.
- **`-e` needs `--write`** unless every statement starts with `SELECT`, `SHOW`, `EXPLAIN` or
  `DESCRIBE` (statements split on `;`), or, for Valkey, every line is a read command (`GET`,
  `INFO`, `SCAN`, `TTL`, `CLIENT LIST`, …; not `CONFIG`). **This is a speed bump against slips,
  not a security boundary.** It does not parse SQL: `SELECT … INTO OUTFILE`, `SELECT` of a
  function with side effects, or Postgres `EXPLAIN ANALYZE DELETE …` all pass without `--write`.
  Leading comments and any backslash (client commands such as `\!`, `\g`, `\copy`) need `--write`.
- Output is the client's own, not JSON: MariaDB prints tab-separated rows with a header, Valkey
  prints raw replies, `psql` prints its aligned table.

## Lifecycle commands

These change runtime state only. Each takes one or more targets; a bare Stack name means every
Service in it. They print the containers they act on, and names are checked before anything
touches the Host.

| Command | Class | What it does |
|---|---|---|
| `hops restart <target…>` | Runtime-reversible | `docker restart` |
| `hops start <target…>` | Runtime-reversible | `docker start` on the existing containers |
| `hops stop <target…> --for <dur> [--force]` | Disruptive | Silences the Service's alerts, then `docker stop` |
| `hops recreate <target…>` | Runtime-reversible | doco-cd force-recreates the Service from the Stack it last deployed |

**`stop`.** A stopped declared Service makes the Host differ from `main`. ContainerDown fires, and
nothing starts the Service again until a reboot, the next Deploy of its Stack, or `hops start`. So:

- `--for` is required (Prometheus duration syntax: `30m`, `2h`, `1d12h`). Before stopping, `stop`
  runs `amtool silence add … --duration=<dur>` (as `alerts` does) once for each way an alert can
  name the Service (`hops/silences.py`):
  - `stack` and `service`: ContainerDown and Probes.
  - `container` and `name`: its container name. This covers TargetDown, ProbeFailed, cAdvisor's
    alerts, and DatastoreDown's `name`.
  - For each static scrape target in `tn/monitoring/prometheus/prometheus.yml` that is scraped at
    the Service's hostname, that target's `container` label (for doco-cd that is `doco-cd`, not
    `doco-cd-doco-cd-1`).
  - For a Datastore, the `datastore` label of its exporter's target.

  It also silences alerts the stop sets off elsewhere, all derived from the repo:
  - Services in the same Stack that `depends_on` it, directly or transitively, get the same sets
    (stopping `vaultwarden-db` silences Vaultwarden's ProbeFailed).
  - An inhibit rule in `alertmanager/config.yml` whose source is the stopped container's own
    ProbeFailed no longer works, because Docker SD drops a stopped container's Probe target. What
    it inhibited is silenced instead. Stopping traefik adds `alertname="ProbeFailed"`, which
    covers all Probes, and IdpForwardAuthFailing.
  - The Stack's notifying rules whose labels name the Stack but no Service get `alertname` and
    `stack` (stopping `stack-back` silences BackupStale). The Watchdog heartbeat is never
    silenced.

  `stop` prints each silence with its reason. If a silence fails, nothing is stopped. Other
  fan-out alerts are left to the inhibit rules, because a silenced source alert still inhibits.
- A Critical Tier Service is refused unless `--force` is passed. For a Stack, one Critical
  Service is enough to refuse.
- `start` does not expire the silences; they run out after `--for`. See `hops alerts`.

**`recreate`** goes through doco-cd's REST API
(`POST http://doco-cd/v1/api/project/<stack>/recreate?service=<service>`). `hops` never runs
`docker compose` itself. The API listens only on doco-cd's networks, so the request is sent from
a transport container on `metrics`:

- The transport is the first running container on `metrics` (prometheus first, then by name)
  whose busybox has `nc -e` and `timeout`, and which is not among the Services being recreated.
  Recreating the transport would cut the request off. If none qualifies, `recreate` refuses
  before decrypting the key or touching any Service.
- `sops -d tn/doco-cd/api_secret.sops.txt` decrypts the API key on this machine.
- The whole HTTP request, key included, goes over ssh's stdin to
  `docker exec -i <transport> sh -c …`, where busybox `nc -e` copies it to doco-cd and the
  response back. The key is never in a process argument on either machine and never written to
  disk on the Host. (`… | nc` would half-close the connection when stdin ends, and doco-cd
  cancels a recreate whose client has gone away.)
- `--timeout` (seconds, default 600) bounds each Service: `timeout` inside the transport ends the
  request, and the ssh process is killed 30 seconds later. Either way `recreate` exits 1 and says
  the recreate may have been interrupted; check with `hops status`.
- Any answer other than 2xx is an error (exit 1) with doco-cd's message. `doco-cd` itself is
  refused, because it replaces itself on its own Deploy.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. `status` returns 0 even when Services are down; read the output for problems. |
| 1 | A Host call failed (SSH unreachable, docker error, no container). `logs` returns `docker logs`' own exit code. |
| 2 | Usage error before any Host call: bad arguments, unknown or ambiguous name, missing config |

`exec` with a command and `db -e` return the remote command's own exit code.

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
- `undeclared`: running containers whose Compose labels match no declared Service (TrueNAS Apps,
  Compose projects named `ix-…`, are left out). `stack` and
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

## Strays

`hops strays` lists the **Strays** (GLOSSARY) on the Host: containers, images, volumes and networks
that no Stack declares or uses. It is read-only unless given `--apply`, and takes `--json`. The rule:
**when ownership is unknown, a Stray is listed, marked `kept`, and never removed.**

| Command | Effect |
|---|---|
| `hops strays` | Lists Strays against **this checkout**, and says so. Changes nothing. |
| `hops strays --apply` | Fetches `origin/main` and classifies against it. Removes Stray containers, then images, then networks. Never volumes. |
| `hops strays --apply --volumes` | Also removes Stray volumes, the only Strays that can hold data. |

`--apply` reads declared state only from `origin/main`, never from the working tree, so a missing or
partial `tn/` cannot make a Stack's data deletable. It fetches with an explicit refspec
(`+refs/heads/main:refs/remotes/origin/main`, whatever `remote.origin.fetch` says), pins the
fetched commit, prints its SHA, and unpacks that commit's Stack root under the git directory. It
refuses when that commit declares no Services.

What counts as a Stray:

- **Container:** its `com.docker.compose.project`/`service` labels match no declared Service, and
  its name is no declared Service's container name. A container deployed under a legacy Compose
  project (`home`, `common`, `media`, `asot-media`) with a declared `container_name` is kept, with
  everything it uses. `--apply` removes only `created`, `exited` and `dead` containers, without
  force; `running`, `paused` and `restarting` ones are kept. Containers labelled `startup-order=skip`
  (deliberate `docker run`s, such as runbook helpers) are marked `deliberate` and otherwise treated
  like any other Stray.
- **Image:** used by no container except Strays that `--apply` removes. An image used by a stopped
  declared container is in use. These are mostly images earlier Deploys and one-off runs left
  behind; Docker pulls any of them again when something needs it. `--apply` removes an image only
  by its ID, never by tag, and without force. An image with several tags is `kept` ("multiple
  tags, remove manually", with its tags): Docker removes one only by force or tag by tag, and a
  concurrent pull can move a tag. The same holds when Docker refuses an ID because it gained tags
  after the list was read; that is not a failure.
- **Volume:** not owned by a declared Stack or by the Compose project of a kept container, not named
  by a Stack (a top-level `volumes:` entry, by `name:` or Compose's `<stack>_<key>`), and not
  mounted by a kept container. A Stack's volume is never a Stray, even while nothing mounts it.
  Only **anonymous volumes without a Compose project label** that nothing references are removed,
  such as those Docker keeps after `compose down` (Compose 2.38 on the Host does not label them with
  the project). One that a Stray container mounts, even one this run removes, is `kept` until a
  later run finds nothing referencing it. A volume labelled with an unmatched Compose project, or a
  named volume with no labels, is listed with `kept`: its ownership is unknown. `--volumes` refuses
  to run while a declared volume name uses a variable, since `hops` cannot tell which volume it
  means.
- **Network:** not owned or named by a declared Stack in the same way, not used by a kept
  container, and not one of Docker's defaults (`bridge`, `host`, `none`). Networks hold no data.
- **Never Strays:** TrueNAS Apps' resources (Compose projects or names starting `ix-`). Build cache
  is out of scope.

A container under an unmatched Compose project is a Stray, not unknown: doco-cd is the only thing
that deploys Stacks on the Host, and Portainer must not own one ([ADR 0001](../../docs/adr/0001-doco-cd-for-gitops.md)),
so such a project is a Stack that was renamed or removed. Its volumes stay `kept` all the same.

Removal is per item and never forced: Docker refuses to remove anything that something started
using since the list was read. A failed or skipped removal is reported on stderr, the rest
continue, and the exit code is 1.

### `strays --json`

```json
{
  "declared_from": "checkout",
  "containers": [
    {"container": "helper", "id": "…", "image": "alpine:3", "state": "exited", "stack": null,
     "service": null, "deliberate": true, "kept": null, "removed": null}
  ],
  "images": [
    {"id": "sha256:…", "repository": "traefik", "refs": ["traefik:v3.6.15"], "size": "188MB",
     "created": "2026-04-29 16:18:27 -0700 PDT", "kept": null, "removed": null}
  ],
  "volumes": [
    {"volume": "retired_data", "stack": "retired", "anonymous": false,
     "kept": "Compose project retired is no declared Stack: ownership unknown", "removed": null}
  ],
  "networks": [{"network": "retired_default", "id": "…", "stack": "retired", "kept": null, "removed": null}]
}
```

- `declared_from`: `checkout`, or `origin/main@<full commit SHA>` with `--apply`.
- `stack`, `service`: the Compose project and service labels, if any.
- `deliberate`: the container carries `startup-order=skip`.
- `kept`: why `--apply` leaves this Stray (its state, unknown ownership, a Stray mounting it, or an
  image's several tags), or `null`.
- `refs`: the image's tags.
- `removed`: `true` or `false` after an `--apply` that tried it, else `null`.

## `bump`: pin a newer image through a PR

```sh
hops bump <service> [<tag>] [--pr] [--trailer "Co-authored-by: …"]
```

A manual Renovate for one Service, for when the 5-day minimum release age is too long. It is
Repo-changing: it edits the repo, never the Host, and the change deploys only after its PR
merges.

- **Tag:** `<tag>` as given, or the newest tag shaped like the current one: same `v` prefix,
  number of version parts and variant suffix (`-alpine`). Pre-releases (`-rc.1`) never qualify.
  This is the rule homelab-components' Canary uses. A tag that is not a version (`latest`,
  `supervised-1.4.27`) has no candidates; name the tag. A candidate more than one major version
  ahead of the pin is refused before anything is created: registries carry stray tags (qbittorrent
  has a 2021 `20.04.1` beside `5.2.4`). `bump` names the rejected tag and the newest acceptable
  one; pass the tag you want explicitly. An explicit tag skips this check.
- **Image digest:** the registry's index (multi-arch) digest, else the manifest digest, exactly what
  Renovate pins (`docker:pinDigests`), so Renovate's next run sees no diff. Anonymous registry v2
  calls; Docker Hub, GHCR, lscr.io and other Bearer-token registries work.
- **Git:** fetches `origin main` and works from `origin/main`, whatever branch you are on: the
  Service and its current pin are read from that tree, and the branch starts there, so the PR
  holds only the bump. It still refuses a checkout with uncommitted or untracked changes, because
  those would silently not be in the bump. Creates branch
  `hops/bump-<service>-<tag>` from `origin/main` in a new worktree
  `.worktrees/bump-<service>-<tag>`, rewrites every `image:` line in the Service's Stack that pins
  the same reference (so `nextcloud` and `nextcloud-cron` move together; other Stacks that pin
  it, such as vaultwarden's `mariadb`, need their own bump), runs
  `tools/validate/validate.py secrets env compose` there, and commits. If validation fails, the
  edit stays uncommitted in the worktree. The checkout you run from is not modified. To retry,
  discard the worktree and branch: `git worktree remove --force <dir>; git branch -D <branch>`.
- **`--pr`:** pushes the branch and runs `gh pr create --base main --body-file -`. Without it
  nothing is pushed; `hops` prints how to push or discard the worktree.
- **Renovate:** if Renovate has an open PR for the same image, `bump` warns, continues, and the
  PR body says "Supersedes #N". Renovate closes its own PR once `main` has that version or newer.
- **Agents:** pass the co-author line with `--trailer` (repeatable; it goes to
  `git commit --trailer`), e.g.
  `--trailer "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"`.

Tests replay recorded registry exchanges from `tests/fixtures/registry/`. To re-record them, see
`tests/hops_registry_fixtures.py`.

## Adding a command

- One module in `hops/commands/` per command. It exposes `register(subparsers)`, which adds a
  parser and calls `set_defaults(func=run)`, where `run(args, ctx) -> int`. Modules are found
  automatically.
- `ctx.declared` (`hops.declared.Declared`) resolves names with `resolve(names)` or
  `resolve_one(name)`. Each `Service` has `tier`, `image` (repository, tag, digest), `container`
  and `datastore`.
- `ctx.runner` (`hops.runner.Runner`) is the only way to reach the Host. `docker(*args)`
  captures output. `pipeline(*commands)` pipes docker commands into each other on the Host,
  under `bash -c 'set -o pipefail; …'`, so it fails if any command fails.
  `attach(*args, tty=)` connects the terminal; interactive modes first call
  `hops.terminal.require_tty(what)`. `hops.docker` reads containers, and
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
