# home-ops

GitOps for the self-hosted Services on a single TrueNAS SCALE Host. Every Stack is a Docker Compose project in this repo. [Renovate](renovate.json) proposes image updates, and [doco-cd](https://doco.cd) deploys whatever is on `main`.

Terms used here (Stack, Service, Datastore, Tier, Page, Digest, ...) are defined in [GLOSSARY.md](GLOSSARY.md). Design decisions are recorded in [docs/adr](docs/adr).

## Layout

| Path | Contents |
|---|---|
| `tn/<stack>/compose.yaml` | One Stack per folder; the folder name is the Compose project name |
| `tn/<stack>/.sops.env` | SOPS-encrypted interpolation values for the Stack (required) |
| `tn/<stack>/env.template` | The keys `.sops.env` must contain, with empty values |
| `.doco-cd.yml` | How doco-cd discovers and deploys Stacks |
| `host/` | One-off TrueNAS setup scripts (users and groups) |
| `.github/scripts/validate.py` | The checks CI runs on every PR |

## Agent setup

Agent workflows are pinned to an upstream commit in
[`.agent-skills.lock`](.agent-skills.lock), rather than vendored in this repo.
With Bash, Git, and Python 3 installed, run once per clone:

```sh
scripts/setup-agent-skills
scripts/setup-agent-skills check
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
| `monitoring` | Prometheus, Alertmanager, Grafana, Loki, Alloy, blackbox-exporter (Probes), docker-socket-proxy, cAdvisor, node-exporter, dcgm-exporter, restic-exporter, backup-coverage |
| `nextcloud` | Nextcloud (+ cron), MariaDB (+ mysqld_exporter), Valkey (+ redis_exporter) |
| `vaultwarden` | Vaultwarden, its own MariaDB (+ mysqld_exporter) |
| `open-webui` | Open WebUI, Ollama |
| `syncthing` | Syncthing |
| `arr` | Sonarr, Radarr (+ queue exporter), SABnzbd (+ Usenet server status exporter), qBittorrent, autobrr, dependency-probe (Dependency Probes) |
| `plex`, `emby`, `nextpvr`, `navidrome`, `tracearr` | One media Service each |

### Shared networks

Each Stack keeps its Datastores on its own `default` network. Only these networks span Stacks (ADR-0002):

| Network | Owned by | Joined by |
|---|---|---|
| `proxy` (172.30.0.0/24; dynamic .128/25) | `edge` | Traefik and every Exposed Service |
| `metrics` (172.29.0.0/24) | `monitoring` | Prometheus and the exporters it scrapes |
| `backup` | `ops` | stack-back and the databases it dumps |

Other Stacks declare these networks `external: true`, so `edge`, `monitoring` and `ops` must come up first on a fresh Host. Before that, other Stacks' deploys fail and doco-cd retries them.

## How changes reach the Host

1. Open a PR. CI runs `validate.py` (secrets, env, compose, monitoring configs, Cloudflare IPs), plus gitleaks, actionlint and the Renovate config validator.
2. Merge to `main`.
3. Within 2 minutes doco-cd pulls `main`, decrypts each Stack's `.sops.env` and referenced `*.sops.*` files, and runs `docker compose up` for every Stack that changed. A failed deploy is retried with a forced recreate of that Stack.
4. Results go to Discord through Apprise. Repeated failures raise the `DeployFailing` alert.

Deleting a Stack folder removes the Stack from the Host but keeps its volumes. Volumes that predate the one-Stack-per-app split keep their old names (e.g. `home_vaultwarden_data`) through an explicit `name:`; don't rename them without a planned data move (ADR-0004).

Renovate runs Friday mornings (America/Los_Angeles), pins digests, and waits 5 days after a release before proposing it.

## Secrets

- Every secret is SOPS-encrypted with age. The public key is in `.sops.yaml`.
- The private key lives on the Host at `/mnt/luna/homeops/age.key` (root, 0400) and in an offline backup. Nowhere else.
- Edit a Stack's values with `sops tn/<stack>/.sops.env`. Add a new key to `env.template` too, or CI fails.
- Files a Compose file mounts or loads (`env_file`, `secrets`) must be named `*.sops.*` and tracked. Plaintext `.env` and `*.txt` files under `tn/` are gitignored.
- doco-cd writes decrypted files as root with mode 0600. A non-root Service must therefore read secrets through `env_file`, not Compose `secrets`.

Validate locally before pushing (needs Docker and `pip install -r .github/scripts/requirements.txt`):

```sh
python3 .github/scripts/validate.py secrets env compose monitoring cloudflare
```

## Monitoring and alerting

Every Service carries a `tier` label (`critical`, `warning` or `info`), and every alert carries `tier` and `stack`.

- **Critical** alerts Page through Pushover.
- **Warning** alerts go to the Discord Digest.
- **Info** is dashboards only.
- A Heartbeat to healthchecks.io catches a dead Alertmanager or Host.

Rules are in `tn/monitoring/prometheus/alerting-rules.yml` and routing in `tn/monitoring/alertmanager/config.yml`. Silence a Stack you are working on (e.g. `alertname=ContainerDown, stack=<stack>`) in Alertmanager.

## Backups

- stack-back runs nightly (10:12 UTC). It dumps every container labeled `stack-back.mariadb: true` over the `backup` network, then backs up all named volumes into a restic repository at `${ZFS_POOL_ROOT_HDD}/docker-backups`.
- A TrueNAS Cloud Sync task pushes that dataset to Backblaze B2 daily at 04:00 (SYNC mode).
- restic-exporter feeds the `BackupStale` and `BackupMetricsMissing` alerts.

## Startup order

Every Service has a `startup-order` label (0 edge, 1 Datastores and monitoring core, 2 Exposed Services, 3 sidecars). `validate.py` rejects a Service without one. `host/startup-order.sh` runs as TrueNAS Init/Shutdown Scripts (System → Advanced): `stop` at SHUTDOWN, `start` at POSTINIT. It logs to `/mnt/luna/homeops/startup/`. Run `plan` for a dry run. See [ADR-0003](docs/adr/0003-startup-order-via-host-scripts.md).

The script is not deployed by doco-cd. After changing it, copy it to `/mnt/luna/homeops/bin/startup-order.sh`.

## Adding a Stack

1. Create `tn/<stack>/compose.yaml`: the Services, plus `include:` for per-Service files if you like.
2. Give every Service `restart: unless-stopped`, `dns_search: .`, a healthcheck where possible, a `tier` label, and a `startup-order` label.
3. For an Exposed Service, join `proxy` (external) and set the `traefik.*` labels including `traefik.docker.network: proxy`. For metrics, join `metrics` and add a scrape job in `prometheus.yml`.
4. For a database, keep it on the Stack's `default` network, label it `stack-back.mariadb: true`, and add it to `backup`.
5. Write `env.template`. Create and encrypt `.sops.env` (it must exist even if empty).
6. Open a PR. doco-cd deploys the Stack once it merges.

## Bootstrapping a fresh Host

1. Run `host/create_usersgroups.sh`. Configure the Apps service so Docker data is on SSD.
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
6. Copy `host/startup-order.sh` to `/mnt/luna/homeops/bin/` (root, 0755). Register `sh /mnt/luna/homeops/bin/startup-order.sh stop` as a SHUTDOWN task and `… start` as a POSTINIT task under System → Advanced → Init/Shutdown Scripts.
