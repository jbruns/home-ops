# photo-migrate

Moves Nextcloud's photo group folder into Immich (ADR 0020, #870). Each file goes to exactly one
account's Photo Collection, and each event folder becomes an album shared with the other account.
Apple-made files are skipped: the phones' iCloud libraries are their source of truth, and #871
reconciles the skipped files against them. Standard library only.

immich-go was considered first and rejected: it cannot skip a file by its EXIF make (only by name
pattern), take a per-file include list, leave some folders out of folder-as-album, or share an album.

## Rules

`plan` decides each file from an exiftool scan. The first skip that applies, in table order, is the reason:

| Decision | Rule |
|---|---|
| owner | The account `config.json`'s `owners` gives the file's top-level folder; otherwise `default_owner` |
| skip `unreadable` | exiftool reports an error other than an unknown file type |
| skip `not-media` | Not an image or video by MIME type (exiftool: unknown file type), or a `.thm` (a camera's thumbnail of a video) |
| skip `unsupported` | Media whose extension Immich does not take (it judges a file by its name) |
| skip `apple-live` | A `.mov` with the same folder and name (any case) as an Apple still: its Live Photo video, whatever its own make. `pair` names the still |
| skip `apple` | Any `Make` tag (EXIF or QuickTime) starts with the word Apple (`Apple`, `APPLE`, `Apple Inc.`), wherever the file is; or the file has no `Make` and its name matches one of `apple_name_patterns` (iOS exports stripped of EXIF) |
| album | The top-level folder, subfolders included, unless it is in `not_albums` (device and person folders). A top-level folder holding no files itself only groups event folders: each child is an album, `<top> / <child>`. Files at the root have none |
| date | EXIF (`DateTimeOriginal`, `CreationDate`, `CreateDate`, `MediaCreateDate`), then the filename (`YY-MM-DD HH-MM-SS …` as the Nextcloud app names uploads, `YYYY-…`, or `YYYYMMDD_HHMMSS`/`YYYYMMDD-HHMMSS` as cameras do), then the file's mtime. Every date carries an offset: an EXIF date's own, else its own EXIF offset tag (`OffsetTimeOriginal` for `DateTimeOriginal`, `OffsetTimeDigitized` for `CreateDate`, EXIF groups only), else UTC for QuickTime's own dates (but local, source `quicktime-local`, when its wall clock is the filename's to the minute: the device wrote local time), else `timezone`'s (DST-aware); filename dates are in `timezone` (UTC for names matching `utc_name_patterns`, which never make a QuickTime date local), and the mtime is shown in it. A local time the zone makes ambiguous takes its first occurrence; one it skips becomes the instant just after the jump; both set `date_uncertain` |

`upload` sends each planned file to its owner. It first asks Immich whether the account already holds
a file with that SHA-1, so a re-run, or an exact duplicate, uploads nothing twice. Immich reads the
EXIF date itself; the planned date is sent as the file's creation and modification time, which Immich
uses when the file has none. A file whose match is in the owner's trash is an error (`trashed`): restore
it in Immich and re-run. Then
each album is created by its owner if missing, given its files, and shared with every other account
as a **viewer** (partner sharing already shows each account the other's files).

## Config

`config.json` (gitignored: it names people's folders) maps folders to accounts. Copy
`config.example.json` and fill in the real folder names. `accounts` maps each account to the `.env`
key holding its API key. `timezone` (an IANA name, default `UTC`) is the household's, for dates that
carry no offset. `quicktime_local_when_name_matches` (default `true`) turns the `quicktime-local` rule on.
`utc_name_patterns` are regular expressions searched in a filename whose date is UTC (iOS exports).
`apple_name_patterns` are regular expressions searched in the filename of a file with
no `Make`.

Each account's API key, created by that account's user, needs the permissions `user.read`, `asset.upload`, `album.read`, `album.create`, `albumAsset.create` and `albumUser.create`; `fix-dates` also needs `asset.read` and `asset.update`.

## Files

Run files live in `tools/photo-migrate/run/` (gitignored). Keep them until #871 is done.

- `exif.json`: the scan.
- `manifest.jsonl`: the plan, one row per file: `path` (relative to the group folder), `action`
  (`upload` or `skip`), `reason`, `owner`, `album`, `media` (`image` or `video`), `date`, `date_source`,
  `date_uncertain`, `date_offset_from_file` (the date's offset is the file's own, not assumed), `make`,
  `size`, `pair`. `upload` stops on any malformed record: re-run `plan`.
- `journal.jsonl`: one row per file `upload` handled: `path`, `owner`, `sha1`, `id` (the Immich asset),
  `status` (`created`, `duplicate`, `trashed`, `owner-mismatch` or `error`), `error`. A file journalled
  `created` or `duplicate` under its planned owner is not sent again; any other is checked again on the
  next run. One journalled as done under another owner is `owner-mismatch` until resolved by hand. A record cut off by an interrupted run is
  dropped, so its file is checked again.

- `fix-dates.jsonl`: one row per asset `fix-dates` checked: `path`, `owner`, `id`, `status`
  (`correct`, `immich-zone`, `would-fix`, `fixed` or `error`), `before` (Immich's `dateTimeOriginal`,
  `timeZone`, `localDateTime`), `after` (the date set), `error`.

**Reconciliation (#871)** reads the manifest's `apple` and `apple-live` rows: each has its owner,
capture `date`, `media` and `album`. To import the files with no match in iCloud, write their rows to a
new manifest with `action` set to `upload` and run `upload` on it with the same journal; their albums
are synced too.

## Run

From the repo root, with `.env` holding the Host's SSH keys and both Immich API keys. The scan runs
Immich's own server image and the upload the Python image another Stack declares, so no image is
pulled and no Stray is left. Both mount `nextcloud_data` read-only.

```sh
set -a; . ./.env; set +a
H="ssh -i $HOPS_TRUENAS_SSH_KEY $HOPS_TRUENAS_SSH_USER@$HOPS_TRUENAS_SSH_HOST sudo -n docker"
GF=/d/data/__groupfolders/<id>     # the photo group folder
R=tools/photo-migrate/run; mkdir -p $R; touch $R/journal.jsonl
IMG=$($H inspect immich-server --format '{{.Image}}')
PY=$(grep -o 'python:[^ ]*' tn/monitoring/prometheus/service.yaml | head -1)

# 1. Scan (read-only, about 10 minutes) and plan; review the report
$H run -i --rm --label startup-order=skip --network none -v nextcloud_data:/d:ro --entrypoint sh "$IMG" \
  -s -- $GF < tools/photo-migrate/scan.sh > $R/exif.json
python3 tools/photo-migrate/photo_migrate.py plan --config tools/photo-migrate/config.json --root $GF \
  < $R/exif.json > $R/manifest.jsonl
python3 tools/photo-migrate/photo_migrate.py report < $R/manifest.jsonl

# 2. Upload, one file at a time (add --limit 0 to only check the keys and network, --limit 20 for a
#    trial); re-run it until no file errors. The journal is appended inside the container, so a dropped
#    SSH session loses nothing. ssh re-splits the command on the Host, so the -c script needs both quote
#    layers. The keys reach the container only as the file keys.env, copied in with docker cp, never
#    through a shell, stdin or argv; the container is removed with it. Try a change with dummy keys first.
#    Follow progress with `$H logs -f photo-migrate`; wait with `$H wait photo-migrate`.
$H create -i --name photo-migrate --label startup-order=skip --network immich_default \
  -v nextcloud_data:/d:ro -w /srv --entrypoint sh "$PY" -c "'python3 photo_migrate.py upload --config config.json \
  --manifest manifest.jsonl --journal journal.jsonl --root $GF --workers 1 --keys-file keys.env >> journal.jsonl'"
COPYFILE_DISABLE=1 tar -c --no-xattrs -C tools/photo-migrate photo_migrate.py config.json -C run manifest.jsonl journal.jsonl \
  | $H cp - photo-migrate:/srv
python3 tools/photo-migrate/photo_migrate.py keys-tar --env .env | $H cp - photo-migrate:/srv
$H start -a photo-migrate
$H cp photo-migrate:/srv/journal.jsonl - | tar -xO > $R/journal.jsonl.new && mv $R/journal.jsonl.new $R/journal.jsonl
$H rm photo-migrate
hops strays
```

`upload` and `fix-dates` read the API keys as `NAME=value` lines from `--keys-file`, else from the
environment, so they never appear in a command line. `upload` prints a journal row per file to stdout and progress and one row per album to stderr (with `failed`,
the files the album refused other than duplicates), and exits 1 if any file failed, is trashed or has
an owner mismatch, or any album failed. After `--max-consecutive-errors` (default 10) file errors in a
row, a systemic failure (a revoked key, a full disk, Immich down), it stops, leaves the albums alone and
exits 2.

## Fix dates

Immich keeps no time zone for an uploaded file's date: it shows a date whose offset `plan` assumed
(filename, mtime, an EXIF or QuickTime date without its own offset) at its UTC wall clock. `fix-dates`
sets `dateTimeOriginal` on each asset `upload` created from such a row to the manifest's instant in
`timezone`, with its offset; Immich stores the offset as the asset's zone and recomputes
`localDateTime` (asynchronously, through its sidecar and metadata jobs) as household wall-clock time.
It skips an asset already right (`correct`) and one whose zone Immich found itself, from GPS say
(`immich-zone`), so a re-run changes nothing. `--dry-run` changes nothing and reports counts; after
`--max-consecutive-errors` (default 10) errors in a row it stops and exits 2.

```sh
$H create --name photo-fix --label startup-order=skip --network immich_default -w /srv --entrypoint sh "$PY" \
  -c "'python3 photo_migrate.py fix-dates --config config.json --manifest manifest.jsonl --journal journal.jsonl \
  --keys-file keys.env --dry-run >> fix-dates.jsonl'"   # drop --dry-run for the real run
COPYFILE_DISABLE=1 tar -c --no-xattrs -C tools/photo-migrate photo_migrate.py config.json -C run manifest.jsonl journal.jsonl \
  | $H cp - photo-fix:/srv
python3 tools/photo-migrate/photo_migrate.py keys-tar --env .env | $H cp - photo-fix:/srv
$H start -a photo-fix
$H cp photo-fix:/srv/fix-dates.jsonl - | tar -xO > $R/fix-dates.jsonl
$H rm photo-fix
```

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/photo-migrate
```

The tests run against a fake Immich; they need no Host, exiftool or Immich.
