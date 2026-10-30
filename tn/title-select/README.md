# Title Select

Title Select audits Source Sets against their DATs (an Owned Application, ADR 0018). It reads `sources/` in the
Games dataset read-only, as the Root `sources`, and keeps its SQLite store and DAT files in the volume
`title-select_store`. Its own README, at the pinned version, documents the commands, API and configuration.

## The workbench

Open `https://titleselect.<domain>/` and sign in through the IdP. The workbench needs no other credential.

- **Upload a DAT:** DAT Revisions → upload, then choose the provider (e.g. `No-Intro`). The DAT is then named
  `<provider>/<DAT header name>`.
- **Register a Source Set:** Source Sets → add, with Root `sources`, the folder's path inside it (e.g.
  `No-Intro/Atari - Atari 2600`) and the DAT. The Source Set takes its folder's name (`Atari - Atari 2600`).
- **Audit:** open the Source Set and start an Audit. Jobs shows its progress live.

## Headless Audit

On the Host, this runs an Audit over the Client Path with the running container's Operator token, so the token never
leaves the Host in plain text:

```sh
sudo docker run --rm --label startup-order=skip --read-only --user 1000:1001 \
  --volumes-from title-select:ro \
  -e TITLE_SELECT_OPERATOR_TOKEN_FILE=/run/secrets/title_select_operator_token \
  "$(sudo docker inspect -f '{{.Config.Image}}' title-select)" \
  audit --server https://titleselect.<domain> --set "Atari - Atari 2600"
```

`--set` takes the Source Set's name, not its path. The image is the one the running container uses. Progress and a summary of the
Findings and Game Statuses go to stderr; the JSON report goes to stdout, so add `> report.json` to keep it. It exits
non-zero if the token is refused, the Service can't be reached, or the Job fails.

## Parking and resuming

Between pilot milestones the Service is parked: `deploy.replicas: 0` on `title-select`, and `tier: info` so
ContainerDown doesn't fire for a declared Service with no container. The Probe labels, routers, volume and secret stay;
Probe targets come from running containers, so a parked Service has none. Backups skip the store while parked (no
container labels it); its contents (DAT Revisions, Source Sets, Audits) can be recreated from the workbench.

To resume, remove `replicas: 0`, set `tier: warning`, run `homelab-declared-containers` and open a PR. doco-cd Deploys
either change. Keep the memory limit, which was measured at the pilot, and re-measure it on a larger Source Set.
