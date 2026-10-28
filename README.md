# Self-hosted Services

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
| `.env.example` | Keys for the gitignored repo-root `.env`: client-side Host access and Repo Tooling config, each with its purpose and consumer |
| `host/` | Run on the Host itself: the startup-order pin and install runbook, and the identities runbook |
| `tools/<name>/` | Repo Tooling (CI and contributor machines only), each with a README: `validate` (the checks CI runs on every PR), `hops` (ops and diagnostics on the Host over SSH), `cloudflare-ips`, `dashboards`, `cwa-hardcover` (sent into the CWA container to match the library to Hardcover), `cwa-rehydrate` (replaces converted EPUBs with Retail EPUBs through Shelfmark), `declared-containers`, `agent-skills` |

## Agent setup

Agent workflows are pinned to an upstream commit in
[`.agent-skills.lock`](.agent-skills.lock), rather than vendored in this repo.
With Bash, Git, and [uv](https://docs.astral.sh/uv/) installed, run once per clone:

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
| `ops` | stack-back (backups) |
| `doco-cd` | doco-cd (self-updating), Apprise for deploy notifications |
| `monitoring` | Prometheus, Alertmanager, Grafana, Loki, Alloy, blackbox-exporter (Probes, Dependency Probes), docker-socket-proxy, cAdvisor, node-exporter, dcgm-exporter, restic-exporter, backup-coverage, container-health-exporter |
| `nextcloud` | Nextcloud (+ cron), MariaDB (+ mysqld_exporter), Valkey (+ redis_exporter) |
| `vaultwarden` | Vaultwarden, its own MariaDB (+ mysqld_exporter) |
| `open-webui` | Open WebUI, Ollama |
| `syncthing` | Syncthing |
| `sonarr`, `radarr` | One Organizer each (+ exportarr, queue exporter) |
| `sabnzbd` | SABnzbd (+ exportarr, Usenet server status exporter) |
| `calibre-web-automated` | Calibre-Web Automated (+ cwa-ingest-gate, which checks each new Book) |
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

1. Open a PR. CI runs `homelab-validate` (secrets, env, compose, monitoring configs, Cloudflare IPs, isolation, privacy) and `homelab-declared-containers --check`, plus gitleaks, actionlint and the Renovate config validator. These are shared Repo Tooling from the Repo Tooling repo ([ADR-0012](docs/adr/0012-shared-repo-tooling.md)), reading the Host from `hosts.yaml`.
2. Merge to `main`.
3. Within 2 minutes doco-cd pulls `main`, decrypts each Stack's `.sops.env` and referenced `*.sops.*` files, and runs `docker compose up` for every Stack that changed. A failed deploy is retried with a forced recreate of that Stack.
4. Results go to Discord through Apprise. Repeated failures raise the `DeployFailing` alert.

Changes to `tn/doco-cd/` ship in their own PR (CI enforces this). doco-cd replaces itself with the `applier` strategy, which waits for in-flight Deploys to finish first. With `scale_out`, two deployers overlapped and left containers named `<id>_<name>`.

To force-recreate one Service (for example after `ContainerLeftoverName`), run `hops recreate <stack>/<service>` (lifecycle commands in the hops README). It calls doco-cd's REST API from a container on the `metrics` network that is not being recreated. The key (`tn/doco-cd/api_secret.sops.txt`) is decrypted locally and sent on stdin.

Deleting a Stack folder removes the Stack from the Host but keeps its volumes. Volumes are named after the Stack that owns them, `<stack>_[<service>_]<purpose>` ([ADR-0010](docs/adr/0010-volumes-named-for-their-stack.md)). Some still carry a prefix from before the one-Stack-per-app split (e.g. `media_plex_config`) until their Stack is moved with `hops volume move` (#638); never rename one by copying.

Renovate runs every morning (America/Los_Angeles) with no hourly PR limit, pins digests, and waits 5 days after a release before proposing it.

## Secrets

- Every secret is SOPS-encrypted with age to two recipients, both listed in `.sops.yaml`: the Host Key and the Operator Key.
- The Host Key lets doco-cd decrypt on the Host. Its private key lives at `/mnt/luna/homeops/age.key` (root, 0400) and in the Operator's offline copy. Nowhere else. That file holds exactly one key, the Host Key.
- The Operator Key lets the Operator edit secrets on their own machine. It never goes on the Host.
- After changing either recipient in `.sops.yaml`, run `sops updatekeys -y` on every `*.sops.*` file under `tn/` and check each lists exactly those two recipients.
- Edit a Stack's values with `sops tn/<stack>/.sops.env`. Add a new key to `env.template` too, or CI fails.
- Files a Compose file mounts or loads (`env_file`, `secrets`) must be named `*.sops.*` and tracked. Plaintext `.env` and `*.txt` files under `tn/` are gitignored.
- Home public addresses are whitelisted in CrowdSec by `tn/edge/crowdsec/home-whitelist.sops.yaml` (a parser, edited with `sops`). Only this Host carries it.
- doco-cd writes decrypted files as root with mode 0600. A non-root Service must therefore read secrets through `env_file`, not Compose `secrets`.
- doco-cd parses `env_file` with the default format only; `format: raw` fails the Deploy. Single-quote values that contain `$`.

Validate locally before pushing. It needs Docker and `homelab-validate` with `homelab-declared-containers` on `PATH` (each documented in its README under `repo-tools/` in the Repo Tooling repo):

```sh
uv tool install "git+https://github.com/jbruns/homelab-components@validate-v1.2.0#subdirectory=repo-tools/validate"
uv tool install "git+https://github.com/jbruns/homelab-components@declared-containers-v1.0.2#subdirectory=repo-tools/declared-containers"
tools/validate/homelab-validate secrets env compose monitoring cloudflare
```

`tools/validate/homelab-validate` runs `homelab-validate` with every input in `tools/validate/paths` passed as `--path`, so a moved or missing component fails its check instead of being skipped. CI, the pre-commit hook and `hops bump` (`hosts.yaml` `validate`) pass the same list; add a line there when the repo gains a component. CI runs the checks through the reusable workflows `.github/workflows/validate.yaml` pins; keep these install tags in step with its `repo-ci` pin.

`privacy` fails CI when prose names a repository, person, login, hostname or domain ([ADR-0013](docs/adr/0013-prose-names-roles-not-names.md)). Its denylist is the `PRIVACY_DENYLIST` repository secret, never committed; locally the check skips unless `HOMELAB_PRIVACY_DENYLIST` holds the same patterns. `tools/validate/privacy-allow` holds the narrow patterns, one per line, that exempt load-bearing text such as install commands; the wrapper and CI pass each as `--privacy-allow`.

`tools/cloudflare-ips`, `tools/cwa-hardcover` and `tools/dashboards` each have a README; `tools/validate/rules` holds the Prometheus rule tests. CI also runs the tools' tests and the `host/` runbook's (`pip install -r tools/requirements-test.txt`):

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

4xx are visible per Service and route kind (`container_route*:traefik_router_requests:rate10m`, from Traefik router metrics, which also count forward-auth and CrowdSec answers) and, for the home public addresses, in the access log on the Service dashboard. There is deliberately no 4xx alert: Internet scanning would make a generic one noise. Alloy labels those access-log lines `client="home"` using `tn/monitoring/alloy/home-clients.sops.env`, which must stay in step with the CrowdSec whitelist (`tn/edge/crowdsec/home-whitelist.sops.yaml`).

Rules are in `tn/monitoring/prometheus/alerting-rules.yml` and routing in `tn/monitoring/alertmanager/config.yml`. Silence a Stack you are working on (e.g. `alertname=ContainerDown, stack=<stack>`) in Alertmanager.

ContainerDown compares desired state with what is running: `homelab-declared-containers` lists every Service in `tn/` as `homeops_container_declared` (node-exporter's textfile collector serves it), and the alert fires for a declared `critical` or `warning` Service whose container cAdvisor has not seen for 2 minutes, after 5 more. Removing a Service or Stack from `tn/` ends its ContainerDown with the deploy; no silence is needed. One-shot helpers and ad-hoc `docker run` containers are not declared, so they never raise it. Regenerate it with `homelab-declared-containers` and commit the file whenever a Service is added, removed or renamed; `homelab-validate monitoring` fails if it is stale.

Grafana dashboards in the folder `grafana/provisioning/dashboards.yml` names are generated by `tools/dashboards/dashboards.py` and are read-only in the UI. Edit the generator, run `python3 tools/dashboards/dashboards.py`, and commit the JSON alongside it; `homelab-validate monitoring` fails if the two disagree. Dashboards reference datasources by the fixed uids in `grafana/provisioning/datasources.yml`; URLs and credentials come from `grafana/datasources.sops.env`. CI parses dashboard PromQL/LogQL and boots Grafana with the same provisioning and dummy secrets. The Overview (Tier overview) is Grafana's home page. All dashboards and datasources come from the repo; anything created in the UI is not kept.

Alert and recording-rule behavioral suites live in `tools/validate/rules/` (`rule-tests` in `tools/validate/paths`) and run with the deployed `promtool` as part of `homelab-validate monitoring`. New or changed rules must include an updated behavioral case; see "Monitoring checks" in the homelab-validate README.

## Backups

- stack-back runs nightly (10:12 UTC). It dumps every container labeled `stack-back.mariadb: true` over the `backup` network, then backs up all named volumes into a restic repository at the root of the Host's own Backblaze B2 bucket, over the S3-compatible API ([ADR-0014](docs/adr/0014-backups-go-straight-to-the-hosts-own-bucket.md)).
- Only named volumes (Service config and state) are backed up. Media on bind mounts under `${ZFS_POOL_ROOT_HDD}` (books, audiobooks, music, ROMs) is not.
- The bucket's lifecycle rule is "Keep only the last version of the file", so B2 deletes what restic prunes a day later.
- `tn/ops/stack-back.sops.env` holds the repository URL, the restic password and stack-back's key, which can write and delete. restic-exporter and backup-coverage read with a separate read-only key (`tn/monitoring/restic-exporter/b2.sops.env`). The Operator's password manager also holds the restic password, since without it no Backup can be read.
- restic-exporter feeds the `BackupStale` and `BackupMetricsMissing` alerts.
- This restic repository is the only Backup; apps' own scheduled backups are part of their state ([ADR-0011](docs/adr/0011-restic-is-the-only-backup.md)).

### Risky changes

Don't copy data before a risky change (no `.bak`, dated or `pre-*` copies). Take a Rollback Snapshot with `hops snapshot take --issue <n>` (#637) and release it once the change is verified; an expired one shows up in `hops strays`. A copy that a snapshot cannot replace is recorded in its issue or PR with a removal checkbox.

## Startup order

Every Service has a `startup-order` label (0 edge, 1 Datastores and monitoring core, 2 Exposed Services, 3 sidecars). `homelab-validate compose` rejects a Service without one. The startup-order Custom Component (`components/startup-order` in the Repo Tooling repo) runs as TrueNAS Init/Shutdown Scripts (System → Advanced): `stop` at SHUTDOWN, `start` at POSTINIT. It keeps its state and logs in `/mnt/luna/homeops/startup/`. For a dry run, run `plan` with that `STARTUP_ORDER_DIR` (step 5 of the runbook linked below). See [ADR-0003](docs/adr/0003-startup-order-via-host-scripts.md).

doco-cd does not deploy it. The Host runs the release pinned in `host/startup-order.pin`, installed at `/mnt/luna/homeops/bin/startup-order.sh`; to install or upgrade it, follow the [runbook](host/README.md#runbook-install-or-upgrade-startup-order).

Label an ad-hoc `docker run` on the Host `startup-order=skip` so startup-order and container-health-exporter leave it alone. It and one-shot helpers are not declared in `tn/`, so they never raise ContainerDown (see Monitoring and alerting).

## Adding a Stack

1. Copy `templates/exposed/` (or `templates/protected/` for a Protected Service) to `tn/<stack>/` and rename `app`. `compose.yaml` is the entry; to split Services out, put each in `<service>/service.yaml` and list it under `include:`.
2. Every Service extends the Baseline (`tn/_baseline.yaml`, [ADR-0007](docs/adr/0007-service-baseline-via-extends.md)). It adds a Compose healthcheck, a digest-pinned image, a memory limit, any `cap_add` it needs (only where the image cannot run as its target user with `user:`), a `tier` label and a `startup-order` label. An item it cannot meet needs a `baseline.exempt.<item>` label giving the reason; `homelab-validate compose` lists them all.
3. For an Exposed Service, join `proxy` (external) and set the `traefik.*` labels including `traefik.docker.network: proxy`. Routers are `<container>-http`, `-https`, `-health` and `-client-<protocol>`; `-health` and `-client-` routers skip forward auth, so they use `strip-idp-headers`, and the Traefik service is named after the container ([ADR-0006](docs/adr/0006-protected-services.md)). For metrics, join `metrics` and add a scrape job in `prometheus.yml`.
4. Give every volume an explicit `name:` of the form `<stack>_[<service>_]<purpose>`. A volume holding data is `external: true`; create it with `docker volume create` before the first Deploy, so Compose can never remove it. One holding only what its Service regenerates (a cache or scratch space) may be left to Compose with the label `homeops.regenerable: "<reason>"`. Cover every `VOLUME` path of the image with a volume, bind mount or `tmpfs`; anonymous volumes fail the Baseline ([ADR-0010](docs/adr/0010-volumes-named-for-their-stack.md)).
5. For a database, keep it on the Stack's `default` network, label it `stack-back.mariadb: true`, and add it to `backup`.
6. Write `env.template`. Create and encrypt `.sops.env` (it must exist even if empty).
7. Run `homelab-declared-containers` and commit `tn/monitoring/node-exporter/declared_containers.prom`, so ContainerDown watches the new Services.
8. Open a PR. doco-cd deploys the Stack once it merges. A change to `tn/_baseline.yaml` recreates every Service that extends it, so it ships in its own PR.

## Bootstrapping a fresh Host

1. Create the users and groups with `host/create_usersgroups.sh` (runbook: [host/README.md](host/README.md)). Configure the Apps service so Docker data is on SSD.
2. Give the Host a second IP. The TrueNAS UI owns 80/443 on the primary address, so Traefik binds to `DOCKER_HOST_IPV4`.
3. Place the Host Key's private key at `/mnt/luna/homeops/age.key` (root, 0400).
4. Bootstrap doco-cd once. After that it manages itself from `tn/doco-cd/`:

```sh
docker volume create doco-cd_data
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v doco-cd_data:/data \
  -v /mnt/luna/homeops/age.key:/run/secrets/sops_age_key:ro \
  -e SOPS_AGE_KEY_FILE=/run/secrets/sops_age_key \
  -e SELF_UPDATE_ENABLED=true \
  -e POLL_CONFIG="- url: <the url in tn/doco-cd/compose.yaml>
  reference: main" \
  <image from tn/doco-cd/compose.yaml> apply-self --bootstrap
```

5. Enroll CrowdSec and create the Traefik bouncer key (`cscli bouncers add traefik`). Store the key as `TRAEFIK_BOUNCER_API_KEY` in `tn/edge/.sops.env`.
6. Install startup-order and register its Init/Shutdown Scripts ([runbook](host/README.md#runbook-install-or-upgrade-startup-order)).
