# Home Operations Repository

This repository contains configurations and scripts for managing a home operations environment, including services for media, home automation, and monitoring.

## Components

*   **`common`**: Common configurations for services like `nginx`, `traefik`, `portainer`, and `whalewall`.
*   **`home`**: Configurations for home automation services like `nextcloud`, `open-webui`, `redis`, `syncthing`, and `vaultwarden`.
*   **`media`**: Configurations for media services like `autobrr`, `emby`, `navidrome`, `nextpvr`, `plex`, `qbittorrent`, `radarr`, `sabnzbd`, `sonarr`, and `tautulli`.
*   **`monitoring`**: Configurations for monitoring services like `alertmanager`, `cadvisor`, `dcgm-exporter`, `grafana`, `loki`, `node-exporter`, `prometheus`, and `promtail`.
*   **`local`**: Configurations for local development and testing.
*   **`tn`**: Configurations intended for a specific TrueNAS environment.

## Setup Instructions

To adapt this repository for your own environment, follow these steps:

1.  **Generate `age` and `sops` keys:**
    *   Create an `age` key for encrypting secrets.
    *   Store the key securely offline.

2.  **Configure environment variables:**
    *   Fill out the `env.template` files in the `common`, `home`, `media`, and `monitoring` directories.
    *   Specify values for:
        *   `cert_resolver`
        *   `domain`
        *   `email`
        *   `tz` (timezone)
        *   `zfs pool root`
        *   `docker host ip`
        *   `common backend subnet`
        *   Cloudflare API key
        *   `traefik_password_hash` (generate with `htpasswd -nbB username password`)

3.  **Encrypt secrets:**
    *   Use `sops` to encrypt the `env.template` files to `.sops.env` files:
        ```bash
        sops --encrypt --in-place .env.template
        ```

4.  **Configure TrueNAS (if applicable):**
    *   Configure the Apps service.
    *   Add an additional IP address.
    *   Create a `docker_backups` dataset.

5.  **Set `SOPS_AGE_KEY`:**
    *   Set the `SOPS_AGE_KEY` environment variable or use the key file directly.

6.  **Decrypt `.sops.env`:**
    *   Decrypt the `.sops.env` files to `.env` files:
        ```bash
        sops --decrypt --in-place .sops.env
        ```

7.  **Bring up `crowdsec`:**
    *   Follow the `crowdsec` documentation to enroll and configure bouncers:
        *   [https://docs.crowdsec.net/docs/cscli/cscli_console_enroll/#cscli-console-enroll](https://docs.crowdsec.net/docs/cscli/cscli_console_enroll/#cscli-console-enroll)
        *   [https://docs.crowdsec.net/docs/local_api/intro/#bouncers](https://docs.crowdsec.net/docs/local_api/intro/#bouncers)
    *   Fill in the `traefik` API key.

8.  **Bring up the stack:**
    *   Use `docker-compose` or `portainer` to bring up the services.

9.  **Configure `portainer`:**
    *   Access `portainer` via `traefik` (or locally on port `9443` if necessary).
    *   Set initial credentials.
    *   Create a directory for `portainer` to unpack the repository to (ownership `root:root`, permissions `chmod -R u=rwX`).

10. **Configure monitoring:**
    *   Fill out the `monitoring/env.template` file.
    *   Configure `alertmanager` in `alertmanager/config.yml` (see [https://github.com/ruanbekker/docker-monitoring-stack-gpnc/tree/main/configs/alertmanager](https://github.com/ruanbekker/docker-monitoring-stack-gpnc/tree/main/configs/alertmanager) for examples).
    *   Configure `alertmanager` web authentication in `alertmanager/web.yml` (see [https://prometheus.io/docs/alerting/latest/https/](https://prometheus.io/docs/alerting/latest/https/) for examples).
    *   Configure `prometheus` web authentication in `prometheus/web.yml` (see [https://github.com/prometheus/prometheus/blob/release-2.55/documentation/examples/web-config.yml](https://github.com/prometheus/prometheus/blob/release-2.55/documentation/examples/web-config.yml) for examples).
    *   Fill in usernames and passwords for `prometheus` and `alertmanager` in `grafana/provisioning/datasources.yml`.

11. **Configure media and home services:**
    *   Fill out the `media/env.template` and `home/env.template` files.
    *   Be aware that services will be in a first-run state and unsecured until configured.

12. **Change file ownership:**
    *   Change ownership of the following directories to the specified users and groups:
        *   `home_*/_data/` to `apps:apps`
        *   `monitoring_*/_data/` to `apps:apps`
        *   `common_*/_data/` to `apps:apps`
        *   `media_emby_*/_data/` to `emby:emby`
        *   `media_navidrome_*/_data/` to `navidrome:navidrome`
        *   `media_nextpvr_*/_data/` to `nextpvr:nextpvr`
        *   `media_plex_*/_data/` to `plex:plex`
        *   `media_radarr_*/_data/` to `radarr:radarr`
        *   `media_sabnzbd_*/_data/` to `sabnzbd:sabnzbd`
        *   `media_sonarr_*/_data/` to `sonarr:sonarr`
        *   `media_tautulli_*/_data/` to `tautulli:tautulli`
        *   `media_xteve_*/_data/` to `xteve:xteve`

13. **Adding/removing a service:**
    *   Create a subdirectory under `stack_name/docker-compose.yaml`.
    *   Edit `stack_name/docker-compose.yaml` to modify includes and backup volumes.
    *   Edit `monitoring/prometheus/prometheus.yml` to modify `scrape_configs`.
    *   (Optional) Cleanup `.env` files.
    *   Modify users and groups as needed.
    *   Modify containers, images, and volumes as needed.

## Forking Instructions

1.  Fork this repository to your own account.
2.  Clone the forked repository to your local machine.
3.  Follow the setup instructions above to configure the repository for your environment.
4.  Customize the configurations and scripts to your liking.
5.  Commit and push your changes to your forked repository.