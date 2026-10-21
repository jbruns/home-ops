# home-ops

GitOps for the self-hosted Services on a single TrueNAS SCALE Host. Every Stack is a Docker Compose project in this repo. [Renovate](renovate.json) proposes image updates, and [doco-cd](https://doco.cd) deploys whatever is on `main`.

Terms used here (Stack, Service, Datastore, Tier, Page, Digest, ...) are defined in [GLOSSARY.md](GLOSSARY.md). Design decisions are recorded in [docs/adr](docs/adr).

## Layout

| Path | Contents |
|---|---|
| `tn/<stack>/compose.yaml` | One Stack per folder; the folder name is the Compose project name |
| `tn/<stack>/<service>/service.yaml` | One Service plus the sidecars that exist only to serve it, listed in the Stack's `include:` |
| `tn/<stack>/.sops.env` | SOPS-encrypted interpolation values for the Stack (required) |
| `tn/<stack>/env.template` | The keys `.sops.env` must contain, with empty values |
| `.doco-cd.yml` | How doco-cd discovers and deploys Stacks |
| `host/` | Run on the Host itself: the startup-order pin and install runbook, and the identities runbook |
| `tools/<name>/` | Repo Tooling (CI and contributor machines only), each with a README: `validate` (the checks CI runs on every PR), `cloudflare-ips`, `dashboards`, `agent-skills` |

## Agent setup

Agent workflows are pinned to an upstream commit in
[`.agent-skills.lock`](.agent-skills.lock), rather than vendored in this repo.
With Bash, Git, and Python 3 installed, run once per clone:

```sh
tools/agent-skills/setup-agent-skills
tools/agent-skills/setup-agent-skills check
```

Start a fresh agent session afterward. Installed skills stay in the gitignored
`.agents/skills/`. See [agent skill setup](docs/agents/skills.md) for prerequisites,
pin updates, selection overrides, and recovery.

## Stacks

| Stack | Services |
|---|---|
| `edge` | Traefik, CrowdSec, nginx (catch-all) |
| `ops` | stack-back (backups), Portainer (viewer here; still deploys asotbz/ops Stacks) |
| `doco-cd` | doco-cd (self-updating), Apprise for deploy notifications |
| `monitoring` | Prometheus, Alertmanager, Grafana, Loki, Alloy, blackbox-exporter (Probes, Dependency Probes), docker-socket-proxy, cAdvisor, node-exporter, dcgm-exporter, restic-exporter, backup-coverage, container-health-exporter |
| `nextcloud` | Nextcloud (+ cron), MariaDB (+ mysqld_exporter), Valkey (+ redis_exporter) |
| `vaultwarden` | Vaultwarden, its own MariaDB (+ mysqld_exporter) |
| `open-webui` | Open WebUI, Ollama |
| `syncthing` | Syncthing |
| `sonarr`, `radarr` | One Organizer each (+ exportarr, queue exporter) |
| `sabnzbd` | SABnzbd (+ exportarr, Usenet server status exporter) |
| `qbittorrent`, `autobrr` | One Service each |
| `plex`, `emby`, `nextpvr`, `navidrome`, `tracearr` | One media Service each |

### Shared networks

Each Stack keeps its Datastores on its own `default` network. Only these networks span Stacks (ADR-0002):

| Network | Owned by | Joined by |
|---|---|---|
| `proxy` (172.30.0.0/24; dynamic .128/25) | `edge` | Traefik and every Exposed Service |
| `metrics` (172.29.0.0/24) | `monitoring` | Prometheus and the exporters it scrapes |
| `downloads` | `edge` | sonarr, radarr, sabnzbd, qbittorrent, autobrr, and blackbox-exporter for Dependency Probes |
| `backup` | `ops` | stack-back and the databases it dumps |

Other Stacks declare these networks `external: true`, so `edge`, `monitoring` and `ops` must come up first on a fresh Host. Before that, other Stacks' deploys fail and doco-cd retries them.

## How changes reach the Host

1. Open a PR. CI runs `validate.py` (secrets, env, compose, monitoring configs, Cloudflare IPs), plus gitleaks, actionlint and the Renovate config validator.
2. Merge to `main`.
3. Within 2 minutes doco-cd pulls `main`, decrypts each Stack's `.sops.env` and referenced `*.sops.*` files, and runs `docker compose up` for every Stack that changed. A failed deploy is retried with a forced recreate of that Stack.
4. Results go to Discord through Apprise. Repeated failures raise the `DeployFailing` alert.

Changes to `tn/doco-cd/` ship in their own PR (CI enforces this). doco-cd replaces itself with the `applier` strategy, which waits for in-flight Deploys to finish first. With `scale_out`, two deployers overlapped and left containers named `<id>_<name>`.

To force-recreate one Service (for example after `ContainerLeftoverName`), call doco-cd's REST API from a container on the `metrics` network. The key is `tn/doco-cd/api_secret.sops.txt`:

```sh
docker exec prometheus wget -qO- --post-data '' --header "x-api-key: $KEY" \
  "http://doco-cd/v1/api/project/<stack>/recreate?service=<service>"
```

Deleting a Stack folder removes the Stack from the Host but keeps its volumes. Volumes that predate the one-Stack-per-app split keep their old names (e.g. `home_vaultwarden_data`) through an explicit `name:`; don't rename them without a planned data move (ADR-0004).

Renovate runs every morning (America/Los_Angeles) with no hourly PR limit, pins digests, and waits 5 days after a release before proposing it.

## Secrets

- Every secret is SOPS-encrypted with age. The public key is in `.sops.yaml`.
- The private key lives on the Host at `/mnt/luna/homeops/age.key` (root, 0400) and in an offline backup. Nowhere else.
- Edit a Stack's values with `sops tn/<stack>/.sops.env`. Add a new key to `env.template` too, or CI fails.
- Files a Compose file mounts or loads (`env_file`, `secrets`) must be named `*.sops.*` and tracked. Plaintext `.env` and `*.txt` files under `tn/` are gitignored.
- Home public addresses are whitelisted in CrowdSec by `tn/edge/crowdsec/home-whitelist.sops.yaml` (a parser, edited with `sops`). Only this Host carries it.
- doco-cd writes decrypted files as root with mode 0600. A non-root Service must therefore read secrets through `env_file`, not Compose `secrets`.
- doco-cd parses `env_file` with the default format only; `format: raw` fails the Deploy. Single-quote values that contain `$`.

Validate locally before pushing (needs Docker and `pip install -r tools/validate/requirements.txt`):

```sh
python3 tools/validate/validate.py secrets env compose monitoring cloudflare
```

Each tool under `tools/` has a README. CI also runs their tests and the `host/` runbook's (`pip install -r tools/requirements-test.txt`):

```sh
python3 -m pytest tools host
```

## Monitoring and alerting

Every Service carries a `tier` label (`critical`, `warning` or `info`), and every alert carries `tier` and `stack`.

- **Critical** alerts Page through Pushover.
- **Warning** alerts go to the Discord Digest.
- **Info** is dashboards only.
- A Heartbeat to healthchecks.io catches a dead Alertmanager or Host.

Prometheus keeps 400 days (capped at 60 GB). Daily Availability per Service is `service:availability:ratio1d` (recording-rules.yml).

4xx are visible per Service and route kind (`container_route:traefik_router_*`, from Traefik router metrics, which also count forward-auth and CrowdSec answers) and, for the home public addresses, in the access log on the Service dashboard. There is deliberately no 4xx alert: Internet scanning would make a generic one noise. Alloy labels those access-log lines `client="home"` using `tn/monitoring/alloy/home-clients.sops.env`, which must stay in step with the CrowdSec whitelist (`tn/edge/crowdsec/home-whitelist.sops.yaml`).

Rules are in `tn/monitoring/prometheus/alerting-rules.yml` and routing in `tn/monitoring/alertmanager/config.yml`. Silence a Stack you are working on (e.g. `alertname=ContainerDown, stack=<stack>`) in Alertmanager.

Grafana dashboards in the `home-ops` folder are generated by `tools/dashboards/dashboards.py` and are read-only in the UI. Edit the generator, run `python3 tools/dashboards/dashboards.py`, and commit the JSON alongside it; `validate.py monitoring` fails if the two disagree. Dashboards reference datasources by the fixed uids in `grafana/provisioning/datasources.yml`; URLs and credentials come from `grafana/datasources.sops.env`. CI parses dashboard PromQL/LogQL and boots Grafana with the same provisioning and dummy secrets. The Overview (Tier overview) is Grafana's home page. All dashboards and datasources come from the repo; anything created in the UI is not kept.

Alert and recording-rule behavioral suites live in `tools/validate/rules/` and run with the deployed `promtool` as part of `validate.py monitoring`. New or changed rules must include an updated behavioral case; see [monitoring validation](tools/validate/README.md#monitoring-checks).

## Backups

- stack-back runs nightly (10:12 UTC). It dumps every container labeled `stack-back.mariadb: true` over the `backup` network, then backs up all named volumes into a restic repository at `${ZFS_POOL_ROOT_HDD}/docker-backups`.
- Only named volumes (Service config and state) are backed up. Media on bind mounts under `${ZFS_POOL_ROOT_HDD}` (books, audiobooks, music, ROMs) is not.
- A TrueNAS Cloud Sync task pushes that dataset to Backblaze B2 daily at 04:00 (SYNC mode).
- restic-exporter feeds the `BackupStale` and `BackupMetricsMissing` alerts.

## Startup order

Every Service has a `startup-order` label (0 edge, 1 Datastores and monitoring core, 2 Exposed Services, 3 sidecars). `validate.py` rejects a Service without one. The [startup-order](https://github.com/jbruns/homelab-components/tree/main/components/startup-order) Custom Component runs as TrueNAS Init/Shutdown Scripts (System → Advanced): `stop` at SHUTDOWN, `start` at POSTINIT. It keeps its state and logs in `/mnt/luna/homeops/startup/`. For a dry run, run `plan` with that `STARTUP_ORDER_DIR` (step 5 of the runbook linked below). See [ADR-0003](docs/adr/0003-startup-order-via-host-scripts.md).

doco-cd does not deploy it. The Host runs the release pinned in `host/startup-order.pin`, installed at `/mnt/luna/homeops/bin/startup-order.sh`; to install or upgrade it, follow the [runbook](host/README.md#runbook-install-or-upgrade-startup-order).

## Adding a Stack

1. Copy `templates/exposed/` (or `templates/protected/` for a Protected Service) to `tn/<stack>/` and rename `app`. `compose.yaml` is the entry; to split Services out, put each in `<service>/service.yaml` and list it under `include:`.
2. Every Service extends the Baseline (`tn/_baseline.yaml`, [ADR-0007](docs/adr/0007-service-baseline-via-extends.md)). It adds a Compose healthcheck, a digest-pinned image, a memory limit, any `cap_add` it needs (only where the image cannot run as its target user with `user:`), a `tier` label and a `startup-order` label. An item it cannot meet needs a `baseline.exempt.<item>` label giving the reason; `validate.py compose` lists them all.
3. For an Exposed Service, join `proxy` (external) and set the `traefik.*` labels including `traefik.docker.network: proxy`. Routers are `<container>-http`, `-https`, `-health` and `-client-<protocol>`; `-health` and `-client-` routers skip forward auth, so they use `strip-idp-headers`, and the Traefik service is named after the container ([ADR-0006](docs/adr/0006-protected-services.md)). For metrics, join `metrics` and add a scrape job in `prometheus.yml`.
4. Give named volumes an explicit `name:`, so moving or renaming the Stack keeps the data.
5. For a database, keep it on the Stack's `default` network, label it `stack-back.mariadb: true`, and add it to `backup`.
6. Write `env.template`. Create and encrypt `.sops.env` (it must exist even if empty).
7. Open a PR. doco-cd deploys the Stack once it merges. A change to `tn/_baseline.yaml` recreates every Service that extends it, so it ships in its own PR.

## Bootstrapping a fresh Host

1. Create the users and groups with `host/create_usersgroups.sh` (runbook: [host/README.md](host/README.md)). Configure the Apps service so Docker data is on SSD.
2. Give the Host a second IP. The TrueNAS UI owns 80/443 on the primary address, so Traefik binds to `DOCKER_HOST_IPV4`.
3. Place the age private key at `/mnt/luna/homeops/age.key` (root, 0400).
4. Bootstrap doco-cd once. After that it manages itself from `tn/doco-cd/`:

```sh
docker volume create doco-cd_data
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v doco-cd_data:/data \
  -v /mnt/luna/homeops/age.key:/run/secrets/sops_age_key:ro \
  -e SOPS_AGE_KEY_FILE=/run/secrets/sops_age_key \
  -e SELF_UPDATE_ENABLED=true \
  -e POLL_CONFIG="- url: https://github.com/jbruns/home-ops.git
  reference: main" \
  <image from tn/doco-cd/compose.yaml> apply-self --bootstrap
```

5. Enroll CrowdSec and create the Traefik bouncer key (`cscli bouncers add traefik`). Store the key as `TRAEFIK_BOUNCER_API_KEY` in `tn/edge/.sops.env`.
6. Install startup-order and register its Init/Shutdown Scripts ([runbook](host/README.md#runbook-install-or-upgrade-startup-order)).
