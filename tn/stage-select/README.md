# Stage Select

Stage Select audits Source Sets against their DATs, repairs them, and builds Selections into Library Folders (an Owned
Application, ADR 0018). It mounts the whole Games dataset read-write at `/data/games`, as one bind (ADR 0009), so a
move between a Source Set, a Library Folder, the Ingest Folder and the Additions Root is a rename. It writes only where
its registered Roots allow: Library Folders in the Root `play`, the Source Sets listed in the Root `sources`'s writable
paths, the Ingest Folder and the Additions Root. It keeps its SQLite store and DAT files in the volume
`stage-select_store`. It has no configuration file: auth comes from the environment in `compose.yaml`, and the Roots
are registered in the store. Its own README, at the pinned version, documents the commands, API and settings.

## Roots

A new store has no Roots; the workbench's setup page (or Settings → Roots) registers them. Register these:

| Root | Path | Kind | Writable |
|---|---|---|---|
| `sources` | `/data/games/sources` | general | no; writable paths: the Source Sets repairs may change (below) |
| `play` | `/data/games/library` | general | yes |
| `ingest` | `/data/games/ingest` | ingest | yes |
| `additions` | `/data/games/additions` | additions | yes |

Never register `/data/games` itself, or `switch/`: the mount reaches them, but no Root should.

## The workbench

Open `https://stageselect.<domain>/` and sign in through the IdP. The workbench needs no other credential.

- **Upload a DAT:** DATs → upload, then choose the provider (e.g. `No-Intro`).
- **Register a Source Set:** in the Root `sources`, the folder's path inside it (e.g. `No-Intro/Atari - Atari 2600`)
  and its DAT.
- **Audit:** open the Source Set and start an Audit. Jobs shows its progress live.
- **Build:** make a Library Folder in the Root `play` (e.g. `roms/atari2600`) bound to the system, Source Set and
  Selection Profile, then plan a build, review the plan and approve it.

## Library Folders

Library Folders live in the Root `play`, `games/library`: today `roms/atari2600`, `roms/gba` and `acceptance/psx`
(the milestone acceptance's PlayStation folder, apart from the hand-laid ES-DE library). Registering one needs a folder
that is empty or missing; Stage Select creates it. A folder an earlier release built keeps its files and its
`.title-select/` folder, and Stage Select won't adopt it: move it aside, register the folder again and build it, then
delete the old folder once you've kept what you want from it. Updating ES-DE's `gamelists/` for the built files (for
example `.a26` to `.zip`) is the Operator's.

## Repairs, ingest and additions

A Source Set that repairs may change is listed in the Root `sources`'s writable paths (Settings → Roots); every other
Source Set stays read-only to Stage Select, though the mount could reach it. The first repair writes a
`.stage-select/marker` into the Source Set.

Drop files into `games/ingest` and scan the Ingest Folder from the workbench. Stage Select moves what it can place into
a Source Set or an additions set through a plan you approve, and keeps the rest aside. Additions sets live in
`games/additions`, one folder per system.

## Headless Audit

On the Host, this runs an Audit over the Client Path with the running container's Operator token, so the token never
leaves the Host in plain text:

```sh
sudo docker run --rm --label startup-order=skip --read-only --user 1000:1001 \
  --volumes-from stage-select:ro \
  -e STAGE_SELECT_OPERATOR_TOKEN_FILE=/run/secrets/stage_select_operator_token \
  "$(sudo docker inspect -f '{{.Config.Image}}' stage-select)" \
  audit --server https://stageselect.<domain> --set "Atari - Atari 2600"
```

`--set` takes the Source Set's name, not its path. The image is the one the running container uses. Progress and a
summary go to stderr; the JSON report goes to stdout, so add `> report.json` to keep it. It exits non-zero if the token
is refused, the Service can't be reached, or the Job fails.

## Parking and resuming

The Service runs (`tier: warning`). To park it between milestones, set `deploy.replicas: 0` on `stage-select` and
`tier: info`, so ContainerDown doesn't fire for a declared Service with no container. The Probe labels, routers, volume
and secret stay; Probe targets come from running containers, so a parked Service has none, and Backups skip the store
while parked. Run `homelab-declared-containers` and open a PR; doco-cd Deploys either change.

The memory limit is twice the largest peak measured (`compose.yaml` notes it). The selection engine's memory grows
with the DAT, so measure again before adding a system with a much larger DAT, and raise the limit if it needs it.
