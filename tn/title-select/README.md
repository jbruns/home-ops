# Title Select

Title Select audits Source Sets against their DATs (an Owned Application, ADR 0018). It reads `sources/` in the
Games dataset read-only, as the Root `sources`, and keeps its SQLite store and DAT files in the volume
`title-select_store`. Its own README, at the pinned version, documents the commands, API and configuration.

## The workbench

Open `https://titleselect.<domain>/` and sign in through the IdP. The workbench needs no other credential.

- **Upload a DAT:** DAT Revisions → upload, then choose the provider (e.g. `No-Intro`). The DAT is then named
  `<provider>/<DAT header name>`.
- **Register a Source Set:** Source Sets → add, with Root `sources`, the folder's path inside it (e.g.
  `No-Intro/Atari - 2600`) and the DAT. The Source Set takes its folder's name (`Atari - 2600`).
- **Audit:** open the Source Set and start an Audit. Jobs shows its progress live.

## Headless Audit

On the Host, this runs an Audit over the Client Path with the running container's Operator token, so the token never
leaves the Host in plain text:

```sh
sudo docker run --rm --label startup-order=skip --read-only --user 1000:1001 \
  --volumes-from title-select:ro \
  -e TITLE_SELECT_OPERATOR_TOKEN_FILE=/run/secrets/title_select_operator_token \
  ghcr.io/jbruns/title-select:0.1.0@sha256:ce62fe1c8d214570e32aade06eddd8669d714f8a60fbc8242cf5683a676a9c88 \
  audit --server https://titleselect.<domain> --set "Atari - 2600"
```

`--set` takes the Source Set's name, not its path. Use the image `compose.yaml` pins. Progress and a summary of the
Findings and Game Statuses go to stderr; the JSON report goes to stdout, so add `> report.json` to keep it. It exits
non-zero if the token is refused, the Service can't be reached, or the Job fails.

## Parking and resuming

Between pilot milestones the Service is parked: a PR sets `deploy.replicas: 0` on `title-select` and removes its
Probe labels. The volume, secret and routers stay. To resume, revert that PR. doco-cd Deploys either change.
