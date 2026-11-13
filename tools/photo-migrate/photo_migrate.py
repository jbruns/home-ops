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
from datetime import datetime


class Config:
    def __init__(self, data):
        self.accounts = dict(data["accounts"])
        self.default_owner = data["default_owner"]
        self.owners = dict(data.get("owners", {}))
        self.not_albums = set(data.get("not_albums", []))


def owner_of(path, config):
    top = path.split("/", 1)[0] if "/" in path else None
    return config.owners.get(top, config.default_owner)


EXIF_DATE_TAGS = ("DateTimeOriginal", "CreationDate", "CreateDate", "MediaCreateDate")
EXIF_DATE = re.compile(r"(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)(\.\d+)?([+-]\d\d:\d\d|Z)?$")
NAME_DATES = (  # YY-MM-DD HH-MM-SS (or YYYY-…), as the Nextcloud app names uploads; YYYYMMDD_HHMMSS, as cameras do
    re.compile(r"(?<!\d)(\d{4}|\d{2})-(\d\d)-(\d\d)[ _-](\d\d)-(\d\d)-(\d\d)(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)"),
)


def tags(record, name):
    """Every value of tag `name`, from any group, in exiftool's order."""
    return [v for k, v in record.items() if k.rsplit(":", 1)[-1] == name]


def _iso(y, mo, d, h, mi, s, frac="", tz=""):
    try:
        datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    if int(y) < 1900:
        return None
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}{frac or ''}{tz or ''}"


def _exif_date(value):
    m = EXIF_DATE.match(str(value).strip())
    return m and _iso(*m.groups())


def resolve_date(record):
    """The capture date and where it came from: EXIF, then the filename, then the file's mtime."""
    for name in EXIF_DATE_TAGS:
        for value in tags(record, name):
            date = _exif_date(value)
            if date:
                return date, "exif"
    name = os.path.basename(record["SourceFile"])
    for pattern in NAME_DATES:
        m = pattern.search(name)
        if m:
            y = m.group(1)
            date = _iso(y if len(y) == 4 else f"{19 if int(y) > 69 else 20}{y}", *m.groups()[1:])
            if date:
                return date, "filename"
    return _exif_date(record.get("System:FileModifyDate", "")), "mtime"


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


def is_apple(record):
    return any(str(v).strip().lower() == APPLE for v in tags(record, "Make"))


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
        if media_of(r) == "image" and is_apple(r):
            apple_stills.setdefault(os.path.splitext(path)[0].lower(), path)
    rows = []
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
        elif is_apple(r):
            reason = APPLE
        else:
            reason = None
        date, source = resolve_date(r)
        rows.append({"path": path, "action": "skip" if reason else "upload", "reason": reason,
                     "owner": owner_of(path, config), "album": album_of(path, config, tops), "media": media,
                     "date": date, "date_source": source, "make": str(makes[0]).strip() if makes else None,
                     "size": r.get("System:FileSize"), "pair": pair})
    return rows


# --- Immich ---

ALBUM_ROLE = "viewer"
ALBUM_BATCH = 500
RETRIES = 3


class ImmichError(Exception):
    pass


class Immich:
    """One account's API client. Each call opens its own connection, so threads can share a client."""

    def __init__(self, server, key):
        url = urllib.parse.urlsplit(server)
        self.host, self.port, self.base = url.hostname, url.port or 80, url.path.rstrip("/") + "/api"
        self.key = key
        self._id = None

    def call(self, method, path, body=None, headers=None, length=None):
        for attempt in range(RETRIES):
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
                if resp.status >= 500 and attempt < RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status >= 400:
                    raise ImmichError(f"{method} {path}: HTTP {resp.status} {data[:300]!r}")
                return resp.status, json.loads(data) if data else None
            except (OSError, http.client.HTTPException) as e:
                if attempt == RETRIES - 1:
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
        """The id of this account's asset with this SHA-1, or None."""
        result = self.call("POST", "/assets/bulk-upload-check", {"assets": [{"id": "f", "checksum": sha1}]})[1]["results"][0]
        if result["action"] == "reject" and result.get("reason") != "duplicate":
            raise ImmichError(f"rejected: {result.get('reason')}")
        return result.get("assetId") if result["action"] == "reject" else None

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


def uploaded(journal):
    """path -> asset id, from journal rows (the latest row for a path wins)."""
    ids = {}
    for j in journal:
        if j.get("id"):
            ids[j["path"]] = j["id"]
    return ids


def upload(rows, root, clients, journal=(), workers=4):
    """Upload every row planned for upload whose path the journal lacks; yield one journal row per file.

    Each file is first checked against its owner's assets by SHA-1, so a file Immich already holds is
    journalled as a duplicate, not sent again.
    """
    done = uploaded(journal)

    def one(r):
        out = {"path": r["path"], "owner": r["owner"], "sha1": None, "id": None, "status": None, "error": None}
        try:
            client, abspath = clients[r["owner"]], os.path.join(root, r["path"])
            out["sha1"] = sha1_of(abspath)
            out["id"] = client.existing(out["sha1"])
            if out["id"]:
                out["status"] = "duplicate"
            else:
                res = client.upload(abspath, r["date"])
                out["id"], out["status"] = res["id"], res["status"]
        except (OSError, ImmichError) as e:
            out["status"], out["error"] = "error", str(e)
        return out

    todo = [r for r in rows if r["action"] == "upload" and r["path"] not in done]
    with ThreadPoolExecutor(workers) as pool:
        yield from pool.map(one, todo)


def sync_albums(rows, journal, clients):
    """Make each (owner, album) hold its uploaded files and share it with every other account; yield one row per album."""
    ids = uploaded(journal)
    wanted = defaultdict(list)
    for r in rows:
        if r["action"] == "upload" and r["album"] and r["path"] in ids:
            wanted[(r["owner"], r["album"])].append(ids[r["path"]])
    for (owner, name), assets in sorted(wanted.items()):
        client = clients[owner]
        others = sorted(c.user_id for a, c in clients.items() if a != owner)
        out = {"owner": owner, "album": name, "id": None, "created": False, "added": 0, "error": None}
        try:
            query = urllib.parse.urlencode({"name": name, "isOwned": "true"})
            album = next((a for a in client.call("GET", f"/albums?{query}")[1] if a["albumName"] == name), None)
            if album is None:
                users = [{"userId": u, "role": ALBUM_ROLE} for u in others]
                album = client.call("POST", "/albums", {"albumName": name, "albumUsers": users})[1]
                out["created"] = True
            out["id"] = album["id"]
            members = {u["user"]["id"] for u in album.get("albumUsers", [])}
            missing = [{"userId": u, "role": ALBUM_ROLE} for u in others if u not in members]
            if missing:
                client.call("PUT", f"/albums/{album['id']}/users", {"albumUsers": missing})
            for i in range(0, len(assets), ALBUM_BATCH):
                res = client.call("PUT", f"/albums/{album['id']}/assets", {"ids": assets[i:i + ALBUM_BATCH]})[1]
                out["added"] += sum(1 for x in res if x.get("success"))
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
        albums = Counter(r["album"] for r in up if r["album"])
        lines.append(f"{owner}  upload {len(up)}  skip {len(mine) - len(up)} (apple stills {skip[(APPLE, 'image')]}, "
                     f"apple videos {skip[(APPLE, 'video')]}, apple live videos {skip['apple-live']}, "
                     f"not-media {skip['not-media']}, unsupported {skip['unsupported']}, unreadable {skip['unreadable']})")
        lines.append(f"{owner}  dates: exif {dates['exif']}, filename {dates['filename']}, mtime {dates['mtime']}")
        lines.append(f"{owner}  albums {len(albums)} ({sum(albums.values())} files)")
        lines += [f"{owner}    album {n!r}: {c}" for n, c in sorted(albums.items())]
    lines += [f"{r['reason']}: {r['path']}" for r in rows if r["reason"] in ("unreadable", "unsupported")]
    return "\n".join(lines)


def read_jsonl(path):
    if not path or not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


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
        rows, journal = read_jsonl(args.manifest), read_jsonl(args.journal)
        if args.limit is not None:
            done = uploaded(journal)
            rows = [r for r in rows if r["action"] == "upload" and r["path"] not in done][:args.limit]
        status = Counter()
        for j in upload(rows, args.root, cl, journal, args.workers):
            status[j["status"]] += 1
            journal.append(j)
            print(json.dumps(j, ensure_ascii=False), flush=True)
            if sum(status.values()) % 500 == 0:
                print(f"files: {dict(status)}", file=sys.stderr, flush=True)
        print(f"files: {dict(status)}", file=sys.stderr)
        if not args.no_albums:
            for a in sync_albums(rows, journal, cl):
                print(json.dumps(a, ensure_ascii=False), file=sys.stderr)
        return 1 if status["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
