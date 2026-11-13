#!/usr/bin/env python3
"""Migrate a folder of photos into Immich, one owning account per file.

Three commands (see README.md):

  plan    exiftool's JSON for the folder to a manifest: per file, upload it (owner, album, date) or skip
          it (Apple-made, its Live Photo video, not media, unreadable). Runs anywhere
  report  a manifest's counts per account. Runs anywhere
  upload  send a manifest's files to their owners' accounts, skipping any the journal or the account
          already holds (by SHA-1), then create and share their albums. Runs where the files and Immich are
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class Config:
    def __init__(self, data):
        self.accounts = dict(data["accounts"])
        self.default_owner = data["default_owner"]
        self.owners = dict(data.get("owners", {}))
        self.not_albums = set(data.get("not_albums", []))
        self.timezone = data.get("timezone", "UTC")
        self.quicktime_local = bool(data.get("quicktime_local_when_name_matches", True))
        self.apple_names = [re.compile(p) for p in data.get("apple_name_patterns", [])]

    @property
    def tz(self):
        """The household's time zone, for dates that carry no offset (loaded only when planning)."""
        return ZoneInfo(self.timezone)


def owner_of(path, config):
    top = path.split("/", 1)[0] if "/" in path else None
    return config.owners.get(top, config.default_owner)


# Each capture-date tag, best first, with the EXIF tag holding its offset when the value lacks one
# (OffsetTime is ModifyDate's, which is no capture date)
EXIF_DATE_TAGS = (("DateTimeOriginal", "OffsetTimeOriginal"), ("CreationDate", None),
                  ("CreateDate", "OffsetTimeDigitized"), ("MediaCreateDate", None))
EXIF_GROUPS = re.compile(r"^(ExifIFD|IFD\d+|SubIFD\d*)$")  # the groups an EXIF offset tag applies to
QUICKTIME_GROUPS = re.compile(r"^(QuickTime|Track\d+)$")  # QuickTime stores its own dates in UTC
EXIF_DATE = re.compile(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)(\.\d+)?\s*([+-]\d\d:\d\d|Z)?$")
OFFSET = re.compile(r"^([+-])(\d\d):(\d\d)$")
NAME_DATES = (  # YY-MM-DD HH-MM-SS (or YYYY-…), as the Nextcloud app names uploads; YYYYMMDD_HHMMSS, as cameras do
    re.compile(r"(?<!\d)(\d{4}|\d{2})-(\d\d)-(\d\d)[ _-](\d\d)-(\d\d)-(\d\d)(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})(\d\d)(\d\d)[-_](\d\d)(\d\d)(\d\d)"),
)


def tags(record, name):
    """Every value of tag `name`, from any group, in exiftool's order."""
    return [v for k, v in record.items() if k.rsplit(":", 1)[-1] == name]


def _tagged(record, name):
    """(group, value) of every tag `name`, in exiftool's order."""
    return [(k.rsplit(":", 1)[0] if ":" in k else "", v) for k, v in record.items() if k.rsplit(":", 1)[-1] == name]


def _offset(value):
    m = OFFSET.match(str(value).strip())
    if not m:
        return None
    sign, h, mi = m.groups()
    delta = timedelta(hours=int(h), minutes=int(mi))
    return timezone(-delta if sign == "-" else delta)


def _datetime(y, mo, d, h, mi, s, frac=None):
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), int(round(float(frac or 0) * 1e6)))
    except ValueError:
        return None
    return dt if dt.year >= 1900 else None


def _iso(dt):
    return dt.isoformat(timespec="milliseconds" if dt.microsecond else "seconds")


def _exif_date(value):
    """A naive or aware datetime from an exiftool date, or None."""
    m = EXIF_DATE.match(str(value).strip())
    if not m:
        return None
    dt = _datetime(*m.groups()[:7])
    if dt and m.group(8):
        dt = dt.replace(tzinfo=timezone.utc if m.group(8) == "Z" else _offset(m.group(8)))
    return dt


def localize(naive, tz):
    """(aware datetime, uncertain) for a wall-clock time in `tz`. An ambiguous time (clocks set back) takes its
    first occurrence; a nonexistent one (clocks set forward) becomes the instant just after the jump."""
    def exists(t):
        return t.replace(tzinfo=tz).astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == t

    first, second = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    if exists(naive):
        return first, first.utcoffset() != second.utcoffset()
    start = naive.replace(second=0, microsecond=0)
    for _ in range(24 * 60):
        if exists(start - timedelta(minutes=1)):
            break
        start -= timedelta(minutes=1)
    return start.replace(tzinfo=tz, fold=0).astimezone(timezone.utc).astimezone(tz), True


def name_date(record):
    """The naive wall-clock time in the file's name, or None."""
    name = os.path.basename(record["SourceFile"])
    for pattern in NAME_DATES:
        m = pattern.search(name)
        if m:
            y = m.group(1)
            dt = _datetime(y if len(y) == 4 else f"{19 if int(y) > 69 else 20}{y}", *m.groups()[1:])
            if dt:
                return dt
    return None


def resolve_date(record, tz, quicktime_local=True):
    """(date, source, uncertain): the capture date, offset-aware, from EXIF, else the filename, else the mtime.

    An EXIF date takes its own offset, else its own EXIF offset tag (EXIF groups only), else UTC for
    QuickTime's own dates, else `tz` (the household's). With `quicktime_local`, a bare QuickTime date whose
    wall clock is the filename's, to the minute, was written in local time: it is read in `tz`
    (`quicktime-local`). A filename date is in `tz`; the mtime is shown in
    `tz`. `uncertain` marks a wall-clock time `tz` makes ambiguous or skips (see `localize`).
    """
    for name, offset_tag in EXIF_DATE_TAGS:
        for group, value in _tagged(record, name):
            dt = _exif_date(value)
            if not dt:
                continue
            uncertain = False
            if dt.tzinfo is None:
                offset = None
                if offset_tag and EXIF_GROUPS.match(group):
                    offset = next((o for o in map(_offset, tags(record, offset_tag)) if o), None)
                elif QUICKTIME_GROUPS.match(group):
                    named = name_date(record) if quicktime_local else None
                    if named and named.replace(second=0, microsecond=0) == dt.replace(second=0, microsecond=0):
                        dt, uncertain = localize(dt, tz)
                        return _iso(dt), "quicktime-local", uncertain
                    offset = timezone.utc
                dt, uncertain = (dt.replace(tzinfo=offset), False) if offset else localize(dt, tz)
            return _iso(dt), "exif", uncertain
    dt = name_date(record)
    if dt:
        dt, uncertain = localize(dt, tz)
        return _iso(dt), "filename", uncertain
    dt = _exif_date(record.get("System:FileModifyDate", ""))
    if dt is None:
        return None, "mtime", False
    dt, uncertain = (dt.astimezone(tz), False) if dt.tzinfo else localize(dt, tz)
    return _iso(dt), "mtime", uncertain


ALBUM_JOINER = " / "


def album_of(path, config, tops_with_files):
    """The event album a file goes into, or None.

    A top-level folder is an album with its subfolders, unless it is in `not_albums`. A top-level folder
    that holds no files itself only groups event folders: each child is an album, named "<top> / <child>".
    `tops_with_files` is the set of top-level folders that hold files directly.
    """
    parts = path.split("/")[:-1]
    if not parts or parts[0] in config.not_albums:
        return None
    if parts[0] in tops_with_files or len(parts) == 1:
        return parts[0]
    return parts[0] + ALBUM_JOINER + parts[1]


APPLE = "apple"
LIVE_VIDEO_EXTS = {".mov"}
UNKNOWN_TYPE = "Unknown file type"  # exiftool's error for a file that is no image or video
NOT_MEDIA_EXTS = {".thm"}  # a camera's thumbnail of a video beside it
# The extensions Immich accepts (its /api/server/media-types, v3.4.1); it judges a file by its name.
IMMICH_EXTS = set("""
.3gp .3gpp .avi .flv .insv .m2t .m2ts .m4v .mkv .mov .mp4 .mpe .mpeg .mpg .mts .mxf .ts .vob .webm .wmv
.avif .bmp .gif .jfif .jpeg .jpg .png .webp .3fr .ari .arw .cap .cin .cr2 .cr3 .crw .dcr .dng .erf .fff .iiq .k25
.kdc .mrw .nef .nrw .orf .ori .pef .psd .raf .raw .rw2 .rwl .sr2 .srf .srw .x3f .heic .heif .hif .insp .jp2 .jpe
.jxl .mpo .svg .tif .tiff
""".split())


def is_apple(record, config):
    """Made by Apple: any Make tag whose first word is "Apple" ("Apple", "APPLE", "Apple Inc."), or, with no
    Make at all, a filename matching one of the config's `apple_name_patterns` (iOS exports stripped of EXIF)."""
    makes = tags(record, "Make")
    if makes:
        return any(re.findall(r"[a-z]+", str(v).lower())[:1] == [APPLE] for v in makes)
    name = os.path.basename(record["SourceFile"])
    return any(p.search(name) for p in config.apple_names)


def media_of(record):
    kind = str(record.get("File:MIMEType", "")).split("/", 1)[0]
    return kind if kind in ("image", "video") else None


def plan(records, root, config):
    """One manifest row per exiftool record: upload it (to whom, into which album, dated how) or skip it (why)."""
    root = root.rstrip("/") + "/"
    rel = [r["SourceFile"][len(root):] if r["SourceFile"].startswith(root) else r["SourceFile"] for r in records]
    tops = {p.split("/", 1)[0] for p in rel if p.count("/") == 1}
    apple_stills = {}
    for path, r in zip(rel, records):
        if media_of(r) == "image" and is_apple(r, config):
            apple_stills.setdefault(os.path.splitext(path)[0].lower(), path)
    tz, rows = config.tz, []
    for path, r in zip(rel, records):
        stem, ext = os.path.splitext(path)
        media, makes = media_of(r), tags(r, "Make")
        pair = apple_stills.get(stem.lower()) if ext.lower() in LIVE_VIDEO_EXTS else None
        errors = [e for e in tags(r, "Error") if e != UNKNOWN_TYPE]
        if errors:
            reason = "unreadable"
        elif media is None or ext.lower() in NOT_MEDIA_EXTS:
            reason = "not-media"
        elif ext.lower() not in IMMICH_EXTS:
            reason = "unsupported"
        elif pair:
            reason = "apple-live"
        elif is_apple(r, config):
            reason = APPLE
        else:
            reason = None
        date, source, uncertain = resolve_date(r, tz, config.quicktime_local)
        rows.append({"path": path, "action": "skip" if reason else "upload", "reason": reason,
                     "owner": owner_of(path, config), "album": album_of(path, config, tops), "media": media,
                     "date": date, "date_source": source, "date_uncertain": uncertain, "make": str(makes[0]).strip() if makes else None,
                     "size": r.get("System:FileSize"), "pair": pair})
    return rows


# --- Immich ---

ALBUM_ROLE = "viewer"
ALBUM_BATCH = 500
RETRIES = 3


class ImmichError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status  # the HTTP status, or None when no response came


class Immich:
    """One account's API client. Each call opens its own connection, so threads can share a client."""

    def __init__(self, server, key):
        url = urllib.parse.urlsplit(server)
        self.host, self.port, self.base = url.hostname, url.port or 80, url.path.rstrip("/") + "/api"
        self.key = key
        self._id = None

    def call(self, method, path, body=None, headers=None, length=None, retries=RETRIES):
        for attempt in range(retries):
            conn = http.client.HTTPConnection(self.host, self.port, timeout=600)
            try:
                h, payload = {"x-api-key": self.key, "Accept": "application/json", **(headers or {})}, None
                if callable(body):
                    h["Content-Length"], payload = str(length), body()
                elif body is not None:
                    h["Content-Type"], payload = "application/json", json.dumps(body).encode()
                conn.request(method, self.base + path, body=payload, headers=h)
                resp = conn.getresponse()
                data = resp.read()
                if resp.status >= 500 and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status >= 400:
                    raise ImmichError(f"{method} {path}: HTTP {resp.status} {data[:300]!r}", resp.status)
                return resp.status, json.loads(data) if data else None
            except (OSError, http.client.HTTPException) as e:
                if attempt == retries - 1:
                    raise ImmichError(f"{method} {path}: {e}") from e
                time.sleep(2 ** attempt)
            finally:
                conn.close()

    @property
    def user_id(self):
        if self._id is None:
            self._id = self.call("GET", "/users/me")[1]["id"]
        return self._id

    def existing(self, sha1):
        """(id, in the trash) of this account's asset with this SHA-1, or (None, False)."""
        result = self.call("POST", "/assets/bulk-upload-check", {"assets": [{"id": "f", "checksum": sha1}]})[1]["results"][0]
        if result["action"] != "reject":
            return None, False
        if result.get("reason") != "duplicate":
            raise ImmichError(f"rejected: {result.get('reason')}")
        return result.get("assetId"), bool(result.get("isTrashed"))

    def album_named(self, name):
        query = urllib.parse.urlencode({"name": name, "isOwned": "true"})
        return next((a for a in self.call("GET", f"/albums?{query}")[1] if a["albumName"] == name), None)

    def find_or_create_album(self, name, users):
        """(album, created). A create whose response is lost is never sent again blind: the album is looked up first."""
        for attempt in range(RETRIES):
            album = self.album_named(name)
            if album:
                return album, False
            try:
                return self.call("POST", "/albums", {"albumName": name, "albumUsers": users}, retries=1)[1], True
            except ImmichError as e:
                if (e.status and e.status < 500) or attempt == RETRIES - 1:
                    raise
                time.sleep(2 ** attempt)

    def upload(self, abspath, date):
        boundary = uuid.uuid4().hex
        stamp = date if re.search(r"(Z|[+-]\d\d:\d\d)$", date) else date + "Z"
        fields = {"fileCreatedAt": stamp, "fileModifiedAt": stamp, "filename": os.path.basename(abspath)}
        head = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items())
        head += (f'--{boundary}\r\nContent-Disposition: form-data; name="assetData"; filename="{fields["filename"]}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n")
        head, tail = head.encode(), f"\r\n--{boundary}--\r\n".encode()

        def body():
            yield head
            with open(abspath, "rb") as f:
                while chunk := f.read(1 << 20):
                    yield chunk
            yield tail
        return self.call("POST", "/assets", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                         len(head) + os.path.getsize(abspath) + len(tail))[1]


def clients(config, server, keys):
    """An Immich client per account, its API key read from `keys` by the env name the config gives."""
    missing = [env for env in config.accounts.values() if not keys.get(env)]
    if missing:
        raise SystemExit(f"missing API keys: {', '.join(missing)}")
    return {account: Immich(server, keys[env]) for account, env in config.accounts.items()}


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


DONE = ("created", "duplicate")


def uploaded(journal):
    """path -> (owner, asset id) of each file the journal records as in Immich (the latest row for a path wins)."""
    done = {}
    for j in journal:
        if j.get("status") in DONE and j.get("id"):
            done[j["path"]] = (j.get("owner"), j["id"])
        elif done.get(j.get("path"), (None,))[0] == j.get("owner"):  # rechecked by its owner and not found
            del done[j["path"]]
    return done


def asset_ids(rows, journal):
    """path -> asset id of each row the journal records as in Immich under the row's owner."""
    done = uploaded(journal)
    return {r["path"]: done[r["path"]][1] for r in rows if done.get(r["path"], (None,))[0] == r["owner"]}


def pending(rows, journal):
    """The rows planned for upload that the journal does not record as in Immich under their owner."""
    ids = asset_ids(rows, journal)
    return [r for r in rows if r["action"] == "upload" and r["path"] not in ids]


def upload(rows, root, clients, journal=(), workers=4):
    """Upload every row planned for upload whose path the journal lacks; yield one journal row per file.

    Each file is first checked against its owner's assets by SHA-1, so a file Immich already holds is
    journalled as a duplicate, not sent again. One whose match is in the trash is journalled as `trashed`,
    a failure: it is checked again on the next run. One the journal records as done under another owner is
    journalled as `owner-mismatch`, a failure, and left alone.
    """
    done = uploaded(journal)

    def one(r):
        out = {"path": r["path"], "owner": r["owner"], "sha1": None, "id": None, "status": None, "error": None}
        if r["path"] in done:
            owner, asset = done[r["path"]]
            out["status"] = "owner-mismatch"
            out["error"] = (f"journalled as asset {asset} of {owner}, but planned for {r['owner']}: "
                            "resolve by hand (move or delete the asset, fix the journal or config)")
            return out
        try:
            client, abspath = clients[r["owner"]], os.path.join(root, r["path"])
            out["sha1"] = sha1_of(abspath)
            out["id"], trashed = client.existing(out["sha1"])
            if trashed:
                out["status"] = "trashed"
                out["error"] = f"its match {out['id']} is in the owner's trash: restore it in Immich, then re-run"
            elif out["id"]:
                out["status"] = "duplicate"
            else:
                res = client.upload(abspath, r["date"])
                out["id"], out["status"] = res["id"], res["status"]
        except (OSError, ImmichError) as e:
            out["status"], out["error"] = "error", str(e)
        return out

    todo = pending(rows, journal)
    with ThreadPoolExecutor(workers) as pool:
        yield from pool.map(one, todo)


def sync_albums(rows, journal, clients):
    """Make each (owner, album) hold its uploaded files and share it with every other account; yield one row per album."""
    ids = asset_ids(rows, journal)
    wanted = defaultdict(list)
    for r in rows:
        if r["action"] == "upload" and r["album"] and r["path"] in ids:
            wanted[(r["owner"], r["album"])].append(ids[r["path"]])
    for (owner, name), assets in sorted(wanted.items()):
        client = clients[owner]
        others = sorted(c.user_id for a, c in clients.items() if a != owner)
        out = {"owner": owner, "album": name, "id": None, "created": False, "added": 0, "failed": [], "error": None}
        try:
            album, out["created"] = client.find_or_create_album(name, [{"userId": u, "role": ALBUM_ROLE} for u in others])
            out["id"] = album["id"]
            members = {u["user"]["id"] for u in album.get("albumUsers", [])}
            missing = [{"userId": u, "role": ALBUM_ROLE} for u in others if u not in members]
            if missing:
                client.call("PUT", f"/albums/{album['id']}/users", {"albumUsers": missing})
            for i in range(0, len(assets), ALBUM_BATCH):
                res = client.call("PUT", f"/albums/{album['id']}/assets", {"ids": assets[i:i + ALBUM_BATCH]})[1]
                out["added"] += sum(1 for x in res if x.get("success"))
                out["failed"] += [{"id": x["id"], "error": x.get("error")} for x in res
                                  if not x.get("success") and x.get("error") != "duplicate"]
        except ImmichError as e:
            out["error"] = str(e)
        yield out


# --- Report and CLI ---

def report(rows):
    """Per account: files to upload, skips by reason, upload date sources, and albums; then each unreadable file."""
    lines = []
    for owner in sorted({r["owner"] for r in rows}, key=lambda o: (o != "operator", o)):
        mine = [r for r in rows if r["owner"] == owner]
        up = [r for r in mine if r["action"] == "upload"]
        skip = Counter((r["reason"], r["media"]) if r["reason"] == APPLE else r["reason"] for r in mine if r["action"] == "skip")
        dates = Counter(r["date_source"] for r in up)
        uncertain_up = sum(1 for r in up if r.get("date_uncertain"))
        uncertain_held = sum(1 for r in mine if r.get("date_uncertain") and r["reason"] in (APPLE, "apple-live"))
        albums = Counter(r["album"] for r in up if r["album"])
        by_name = sum(1 for r in mine if r["reason"] == APPLE and r["media"] == "image" and not r.get("make"))
        lines.append(f"{owner}  upload {len(up)}  skip {len(mine) - len(up)} (apple stills {skip[(APPLE, 'image')]} "
                     f"(by name {by_name}), "
                     f"apple videos {skip[(APPLE, 'video')]}, apple live videos {skip['apple-live']}, "
                     f"not-media {skip['not-media']}, unsupported {skip['unsupported']}, unreadable {skip['unreadable']})")
        lines.append(f"{owner}  dates: exif {dates['exif']}, quicktime-local {dates['quicktime-local']}, filename {dates['filename']}, mtime {dates['mtime']}; "
                     f"uncertain {uncertain_up} to upload, {uncertain_held} held")
        lines.append(f"{owner}  albums {len(albums)} ({sum(albums.values())} files)")
        lines += [f"{owner}    album {n!r}: {c}" for n, c in sorted(albums.items())]
    lines += [f"{r['reason']}: {r['path']}" for r in rows if r["reason"] in ("unreadable", "unsupported")]
    return "\n".join(lines)


def read_manifest(path):
    """The manifest's rows; any malformed record stops the run, so no planned file is silently left out."""
    rows = []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{n}: malformed manifest record ({e}): re-run plan")
            if not isinstance(row, dict) or not {"path", "action", "owner"} <= row.keys():
                raise SystemExit(f"{path}:{n}: manifest record lacks path, action or owner: re-run plan")
            rows.append(row)
    return rows


def read_jsonl(path):
    """Rows of a JSON-lines file. A record cut off by an interrupted write is dropped (its file is checked
    again), and a record appended straight after one is recovered."""
    if not path or not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            starts = [m.start() for m in re.finditer(r'\{"', line)]
            for i in [0] + [i for i in reversed(starts) if i > 0]:
                try:
                    rows.append(json.loads(line[i:]))
                    break
                except json.JSONDecodeError:
                    continue
            else:
                if line:
                    print(f"{path}:{n}: dropped a cut-off record", file=sys.stderr)
    return rows


def read_keys(stream):
    """API keys as NAME=value lines (a .env's), so they never appear in a command line."""
    keys = {}
    for line in stream:
        name, sep, value = line.strip().partition("=")
        if sep and not name.startswith("#"):
            keys[name.strip()] = value.strip().strip("'\"")
    return keys


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="exiftool -j -G1 -a -n output (stdin) to a manifest, one JSON line per file (stdout)")
    p.add_argument("--config", required=True)
    p.add_argument("--root", required=True, help="the scanned folder, as exiftool saw it")
    sub.add_parser("report", help="manifest (stdin) to per-account counts (stdout)")
    u = sub.add_parser("upload", help="upload a manifest's files and sync its albums; API keys as NAME=value lines on stdin; "
                                      "new journal rows to stdout")
    u.add_argument("--config", required=True)
    u.add_argument("--manifest", required=True)
    u.add_argument("--journal", help="journal of earlier runs; its files are not uploaded again")
    u.add_argument("--root", required=True, help="the folder the manifest's paths are relative to")
    u.add_argument("--server", default="http://immich-server:2283")
    u.add_argument("--workers", type=int, default=4)
    u.add_argument("--limit", type=int, help="upload at most this many files (a trial run)")
    u.add_argument("--no-albums", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "plan":
        with open(args.config) as f:
            config = Config(json.load(f))
        for r in plan(json.load(sys.stdin), args.root, config):
            print(json.dumps(r, ensure_ascii=False))
    elif args.cmd == "report":
        print(report([json.loads(line) for line in sys.stdin if line.strip()]))
    elif args.cmd == "upload":
        with open(args.config) as f:
            config = Config(json.load(f))
        cl = clients(config, args.server, read_keys(sys.stdin))
        for account, client in cl.items():  # fail fast on a wrong key, missing permission or server
            try:
                client.user_id
            except ImmichError as e:
                raise SystemExit(f"account {account}: {e}")
            print(f"account {account}: ok", file=sys.stderr)
        rows, journal = read_manifest(args.manifest), read_jsonl(args.journal)
        if args.limit is not None:
            rows = pending(rows, journal)[:args.limit]
        status = Counter()
        for j in upload(rows, args.root, cl, journal, args.workers):
            status[j["status"]] += 1
            journal.append(j)
            print(json.dumps(j, ensure_ascii=False), flush=True)
            if sum(status.values()) % 500 == 0:
                print(f"files: {dict(status)}", file=sys.stderr, flush=True)
        print(f"files: {dict(status)}", file=sys.stderr)
        album_failures = 0
        if not args.no_albums:
            for a in sync_albums(rows, journal, cl):
                album_failures += bool(a["error"] or a["failed"])
                print(json.dumps(a, ensure_ascii=False), file=sys.stderr)
            print(f"albums failed: {album_failures}", file=sys.stderr)
        return 1 if status["error"] or status["trashed"] or status["owner-mismatch"] or album_failures else 0


if __name__ == "__main__":
    sys.exit(main())
