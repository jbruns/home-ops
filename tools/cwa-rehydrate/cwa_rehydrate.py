#!/usr/bin/env python3
"""Replace converted EPUBs in the CWA Books Library with Retail EPUBs from Anna's Archive (ADR 0016, #755).

Commands (see README.md for where each runs):

  plan    read the library (read-only); print the counts, then one JSON line per Book without a Retail EPUB
  fetch   for each planned Book, download up to --tries Anna's Archive EPUBs into staging and put the first
          Retail EPUB in the Ingest Folder; the ingest gate merges it into the Book. Resumable.
  report  the availability report (Markdown) and the list to fetch by hand (CSV)

A converted EPUB shows what conversion leaves in its content: calibre's page_styles.css, index_split files or
calibre classes. Calibre named as producer alone only means it edited the file.
"""

import argparse
import collections
import csv
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

KINDS = ("no epub", "broken epub", "converted epub")
COUNTS = ("books", "retail epub", "converted epub", "broken epub", "no epub", "no hardcover id")
# Results whose Book still lacks a Retail EPUB: the Operator fetches these by hand
FALLBACK = ("no retail", "converted ingested", "no epub", "no hardcover id", "failed")
USER_AGENT = {"User-Agent": "cwa-rehydrate"}


class DownloadError(Exception):
    """One download failed; the next release may still work."""


class QuotaError(DownloadError):
    """Anna's Archive has no fast downloads left today."""


def inspect_epub(path):
    """Retail or converted, the markers found, and the declared language; broken if unreadable."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            opf = next((n for n in names if n.endswith(".opf")), None)
            opf_text = z.read(opf).decode("utf8", "replace") if opf else ""
            markers = []
            if any(n.endswith("page_styles.css") for n in names):
                markers.append("page_styles.css")
            if any(re.search(r"index_split_\d+", n) for n in names):
                markers.append("index_split")
            pages = [n for n in names if n.endswith((".html", ".xhtml", ".htm"))]
            if pages and re.search(r'class="calibre\d*"', z.read(pages[len(pages) // 2]).decode("utf8", "replace")):
                markers.append("calibre classes")
            lang = re.search(r"<dc:language[^>]*>\s*([^<\s]+)", opf_text)
            return {"retail": not markers, "markers": markers, "lang": lang.group(1).casefold() if lang else None,
                    "broken": False}
    except (zipfile.BadZipFile, OSError, KeyError):
        return {"retail": False, "markers": ["unreadable"], "lang": None, "broken": True}


def english(lang):
    return lang is None or lang in ("en", "eng") or lang.startswith("en-")


# --- plan --------------------------------------------------------------------------------------

def plan(library):
    """Counts for the whole library, and the Books without a Retail EPUB, Books with no EPUB first."""
    library = Path(library)
    db = sqlite3.connect(f"file:{library / 'metadata.db'}?mode=ro", uri=True)
    ids = {b: {} for (b,) in db.execute("select id from books")}
    for b, t, v in db.execute("select book, type, val from identifiers where type in ('hardcover-id', 'isbn')"):
        ids.setdefault(b, {})[t] = v
    authors = collections.defaultdict(list)
    for b, name in db.execute("select l.book, a.name from books_authors_link l join authors a on a.id = l.author "
                              "order by l.id"):
        authors[b].append(name)
    files = collections.defaultdict(list)
    for b, fmt, name in db.execute("select book, format, name from data"):
        files[b].append((fmt, name))
    counts = dict.fromkeys(COUNTS, 0)
    cands = []
    for b, title, path in db.execute("select id, title, path from books order by id"):
        counts["books"] += 1
        hc = ids.get(b, {}).get("hardcover-id")
        counts["no hardcover id"] += not hc
        epub = next((name for fmt, name in files[b] if fmt == "EPUB"), None)
        markers = []
        if epub is None:
            kind = "no epub"
        else:
            info = inspect_epub(library / path / f"{epub}.epub")
            markers = info["markers"]
            kind = "broken epub" if info["broken"] else "retail epub" if info["retail"] else "converted epub"
        counts[kind] += 1
        if kind != "retail epub":
            cands.append({"id": b, "title": title, "authors": " & ".join(authors[b]), "kind": kind,
                          "hardcover_id": hc, "isbn": ids.get(b, {}).get("isbn"),
                          "formats": sorted(f for f, _ in files[b]), "markers": markers})
    cands.sort(key=lambda c: (KINDS.index(c["kind"]), c["id"]))
    return counts, cands


# --- fetch -------------------------------------------------------------------------------------

def done_ids(attempts_path):
    p = Path(attempts_path)
    return {json.loads(x)["id"] for x in p.read_text().splitlines() if x.strip()} if p.exists() else set()


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip()[:120] or "book"


def fetch(cands, *, search, download, ingest, staging, attempts_path, tries=3, reserve=100, limit=None,
          backlog=5, pace=0, wait=time.sleep):
    """Try each planned Book not yet in attempts_path; return "done", "limit" or "quota".

    search(hardcover_id) -> Shelfmark releases; download(release, dest) -> fast downloads left (or None).
    A Book starts only when all its tries fit above the reserve of fast downloads.
    """
    ingest, staging = Path(ingest), Path(staging)
    done, left, started = done_ids(attempts_path), None, 0
    for c in cands:
        if c["id"] in done:
            continue
        if limit is not None and started >= limit:
            return "limit"
        if left is not None and left - tries < reserve:
            return "quota"
        started += 1
        row = dict(c, tries=[])
        if not c.get("hardcover_id"):
            row["result"] = "no hardcover id"
            record(attempts_path, row)
            continue
        releases = [r for r in search(c["hardcover_id"]) if r.get("source") == "direct_download"]
        row["available"] = dict(collections.Counter(f"{r.get('format')} {r.get('language')}" for r in releases))
        epubs = [r for r in releases if r.get("format") == "epub" and r.get("language") == "en"]
        if not epubs:
            row["result"] = "no epub"
            record(attempts_path, row)
            continue
        fallback = None
        try:
            for rel in epubs[:tries]:
                md5 = rel["source_id"]
                dest = staging / f"{md5}.epub"
                try:
                    if pace:
                        wait(pace)
                    left = download(rel, dest)
                except QuotaError:
                    dest.unlink(missing_ok=True)
                    raise
                except DownloadError as e:
                    dest.unlink(missing_ok=True)
                    row["tries"].append({"md5": md5, "error": str(e)})
                    continue
                info = inspect_epub(dest)
                row["tries"].append({"md5": md5, "retail": info["retail"], "markers": info["markers"],
                                     "lang": info["lang"]})
                usable = not info["broken"] and english(info["lang"])
                if usable and info["retail"]:
                    put_in_ingest(dest, ingest, c["title"], md5, backlog, wait)
                    row["result"], row["ingested"] = "retail", md5
                    break
                if usable and fallback is None and c["kind"] != "converted epub":
                    fallback = staging / f"{md5}.keep"
                    dest.rename(fallback)
                    row["fallback"] = md5
                else:
                    dest.unlink()
        except QuotaError:
            if fallback:
                fallback.unlink()
            return "quota"
        if "result" not in row:
            if fallback:
                put_in_ingest(fallback, ingest, c["title"], row["fallback"], backlog, wait)
                row["result"], row["ingested"] = "converted ingested", row.pop("fallback")
            else:
                row["result"] = "no retail" if any("retail" in t for t in row["tries"]) else "failed"
        elif fallback:
            fallback.unlink()
            row.pop("fallback")
        record(attempts_path, row)
    return "done"


def put_in_ingest(src, ingest, title, md5, backlog, wait):
    """Copy into the Ingest Folder once CWA has fewer than `backlog` EPUBs waiting there, then drop the staged file.

    A copy writes the file under its final name and closes it once, so CWA sees one finished file.
    """
    while len(list(ingest.glob("*.epub"))) >= backlog:
        wait(30)
    shutil.copyfile(src, ingest / f"{safe_name(title)} - {md5}.epub")
    src.unlink()


def record(attempts_path, row):
    with open(attempts_path, "a") as f:
        f.write(json.dumps(row) + "\n")


def shelfmark_search(base="http://127.0.0.1:8084", config="/config"):
    """Search Shelfmark's releases for a Hardcover book as its first admin, with a session its own key signs."""
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface

    app = Flask("cwa-rehydrate")
    app.secret_key = Path(config, ".flask_secret").read_bytes()
    users = sqlite3.connect(f"file:{config}/users.db?mode=ro", uri=True)
    uid, name = users.execute("select id, username from users order by id limit 1").fetchone()
    cookie = SecureCookieSessionInterface().get_signing_serializer(app).dumps(
        {"user_id": name, "is_admin": True, "db_user_id": uid})

    def search(hardcover_id):
        q = urllib.parse.urlencode({"provider": "hardcover", "book_id": hardcover_id, "source": "direct_download",
                                    "languages": "en"})
        req = urllib.request.Request(f"{base}/api/releases?{q}", headers={"Cookie": f"shelfmark_session={cookie}"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r).get("releases") or []

    return search


def aa_download(key):
    """Download a release through Anna's Archive's fast-download API. Errors never carry a URL (it holds the key)."""

    def download(rel, dest):
        u = urllib.parse.urlsplit(rel["info_url"])
        api = f"{u.scheme}://{u.netloc}/dyn/api/fast_download.json?" + urllib.parse.urlencode(
            {"md5": rel["source_id"], "key": key})
        try:
            with urllib.request.urlopen(urllib.request.Request(api, headers=USER_AGENT), timeout=60) as r:
                info = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                info = json.load(e)
            except ValueError:
                raise DownloadError(f"fast_download HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise DownloadError(f"fast_download {type(e).__name__}") from None
        err = str(info.get("error") or "")
        if "no downloads left" in err.casefold():
            raise QuotaError(err)
        if not info.get("download_url"):
            raise DownloadError(f"fast_download: {err or 'no download_url'}")
        try:
            req = urllib.request.Request(info["download_url"], headers=USER_AGENT)
            with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
        except urllib.error.HTTPError as e:
            raise DownloadError(f"download HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise DownloadError(f"download {type(e).__name__}") from None
        return (info.get("account_fast_download_info") or {}).get("downloads_left")

    return download


# --- report ------------------------------------------------------------------------------------

def report(before, after, rows):
    """The availability report as Markdown, and the Books to fetch by hand."""
    out = ["| Count | Before | After | Change |", "|---|---|---|---|"]
    out += [f"| {k} | {before[k]} | {after[k]} | {after[k] - before[k]:+d} |" for k in COUNTS]
    out += ["", "| Result | Books |", "|---|---|"]
    out += [f"| {k} | {n} |" for k, n in collections.Counter(r["result"] for r in rows).most_common()]
    no_epub = collections.Counter(k for r in rows if r["result"] == "no epub" for k in r.get("available", {}))
    out += ["", "Books with no English EPUB on Anna's Archive, by the Formats it had:", "",
            "| Format | Books |", "|---|---|"]
    out += [f"| {k} | {n} |" for k, n in no_epub.most_common()] or ["| none | 0 |"]
    fallback = [{"id": r["id"], "title": r["title"], "authors": r["authors"], "kind": r["kind"],
                 "result": r["result"], "available": ", ".join(f"{k} {n}" for k, n in r.get("available", {}).items())}
                for r in rows if r["result"] in FALLBACK]
    return "\n".join(out) + "\n", fallback


def read_plan(path):
    lines = [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
    return lines[0]["counts"], lines[1:]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--library", default="/calibre-library")
    f = sub.add_parser("fetch")
    f.add_argument("plan", help="output of plan")
    f.add_argument("--attempts", required=True, help="JSON lines, appended; a rerun skips Books already in it")
    f.add_argument("--ingest", default="/cwa-book-ingest")
    f.add_argument("--staging", required=True)
    f.add_argument("--tries", type=int, default=3)
    f.add_argument("--reserve", type=int, default=100, help="fast downloads to leave for readers' requests")
    f.add_argument("--limit", type=int, help="Books to try in this run")
    f.add_argument("--backlog", type=int, default=5, help="wait while the Ingest Folder holds this many EPUBs")
    f.add_argument("--pace", type=float, default=10, help="seconds between downloads")
    r = sub.add_parser("report")
    r.add_argument("before")
    r.add_argument("after")
    r.add_argument("attempts")
    r.add_argument("--fallback", required=True, help="CSV of Books to fetch by hand")
    a = ap.parse_args(argv)

    if a.cmd == "plan":
        counts, cands = plan(a.library)
        print(json.dumps({"counts": counts}))
        for c in cands:
            print(json.dumps(c))
        print(json.dumps(counts), file=sys.stderr)
    elif a.cmd == "fetch":
        _, cands = read_plan(a.plan)
        key = os.environ.get("AA_DONATOR_KEY")
        if not key:
            sys.exit("AA_DONATOR_KEY is not set")
        Path(a.staging).mkdir(parents=True, exist_ok=True)
        status = fetch(cands, search=shelfmark_search(), download=aa_download(key), ingest=a.ingest,
                       staging=a.staging, attempts_path=a.attempts, tries=a.tries, reserve=a.reserve,
                       limit=a.limit, backlog=a.backlog, pace=a.pace)
        print(json.dumps({"status": status, "done": len(done_ids(a.attempts)), "planned": len(cands)}))
    else:
        before, _ = read_plan(a.before)
        after, _ = read_plan(a.after)
        rows = [json.loads(x) for x in Path(a.attempts).read_text().splitlines() if x.strip()]
        md, fallback = report(before, after, rows)
        sys.stdout.write(md)
        with open(a.fallback, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["id", "title", "authors", "kind", "result", "available"])
            w.writeheader()
            w.writerows(fallback)


if __name__ == "__main__":
    main()
