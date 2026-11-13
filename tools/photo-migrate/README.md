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
| skip `apple` | Any `Make` tag (EXIF or QuickTime) is `Apple`, wherever the file is |
| album | The top-level folder, subfolders included, unless it is in `not_albums` (device and person folders). A top-level folder holding no files itself only groups event folders: each child is an album, `<top> / <child>`. Files at the root have none |
| date | EXIF (`DateTimeOriginal`, `CreationDate`, `CreateDate`, `MediaCreateDate`), then the filename (`YY-MM-DD HH-MM-SS …` as the Nextcloud app names uploads, `YYYY-…`, or `YYYYMMDD_HHMMSS` as cameras do), then the file's mtime |

`upload` sends each planned file to its owner. It first asks Immich whether the account already holds
a file with that SHA-1, so a re-run, or an exact duplicate, uploads nothing twice. Immich reads the
EXIF date itself; the planned date is sent as the file's creation and modification time, which Immich
uses when the file has none (a date without an offset is sent as UTC, so it shows as written). Then
each album is created by its owner if missing, given its files, and shared with every other account
as a **viewer** (partner sharing already shows each account the other's files).

## Config

`config.json` (gitignored: it names people's folders) maps folders to accounts. Copy
`config.example.json` and fill in the real folder names. `accounts` maps each account to the `.env`
key holding its API key.

Each account's API key, created by that account's user, needs the permissions `user.read`, `asset.upload`, `album.read`, `album.create`, `albumAsset.create` and `albumUser.create`.

## Files

Run files live in `tools/photo-migrate/run/` (gitignored). Keep them until #871 is done.

- `exif.json`: the scan.
- `manifest.jsonl`: the plan, one row per file: `path` (relative to the group folder), `action`
  (`upload` or `skip`), `reason`, `owner`, `album`, `media` (`image` or `video`), `date`, `date_source`,
  `make`, `size`, `pair`.
- `journal.jsonl`: one row per file `upload` handled: `path`, `owner`, `sha1`, `id` (the Immich asset),
  `status` (`created`, `duplicate` or `error`), `error`. A file with an `id` is not sent again.

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

# 2. Upload (add --limit 0 to only check the keys and network, --limit 20 for a trial); re-run it until no file errors
$H create -i --name photo-migrate --label startup-order=skip --network immich_default \
  -v nextcloud_data:/d:ro -w /srv "$PY" \
  python3 photo_migrate.py upload --config config.json --manifest manifest.jsonl --journal journal.jsonl --root $GF
COPYFILE_DISABLE=1 tar -c --no-xattrs -C tools/photo-migrate photo_migrate.py config.json -C run manifest.jsonl journal.jsonl \
  | $H cp - photo-migrate:/srv
grep '^IMMICH_API_KEY_' .env | $H start -ai photo-migrate >> $R/journal.jsonl
$H rm photo-migrate
hops strays
```

`upload` reads the API keys as `NAME=value` lines on stdin, so they never appear in a command line,
prints a journal row per file to stdout and progress and one row per album to stderr, and exits 1 if
any file failed.

## Test

```sh
python3 -m pip install -r tools/requirements-test.txt
python3 -m pytest tools/photo-migrate
```

The tests run against a fake Immich; they need no Host, exiftool or Immich.
