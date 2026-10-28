#!/usr/bin/env python3
"""Replace converted EPUBs in the CWA Books Library with Retail EPUBs from Anna's Archive (ADR 0016, #755).

Commands (see README.md for where each runs):

  plan    read the library (read-only); print the counts, then one JSON line per Book without a Retail EPUB
  fetch   for each planned Book, download up to --tries Anna's Archive EPUBs into staging and put the first
          Retail EPUB of the same Book, stamped with the Book's identity, in the Ingest Folder. Resumable.
  report  the availability report (Markdown) and the list to fetch by hand (CSV)

A converted EPUB shows what conversion leaves in its content: calibre's page_styles.css, index_split files or
calibre classes. Calibre named as producer alone only means it edited the file.
"""

import argparse
import html
import collections
import csv
import hashlib
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
import uuid
import zipfile
from pathlib import Path

KINDS = ("no epub", "broken epub", "converted epub")
COUNTS = ("books", "retail epub", "converted epub", "broken epub", "no epub", "no hardcover id")
USER_AGENT = {"User-Agent": "cwa-rehydrate"}


class DownloadError(Exception):
    """One download failed; the next release may still work."""


class QuotaError(DownloadError):
    """Anna's Archive has no fast downloads left today."""


def inspect_epub(path):
    """Retail or converted, the markers found, the declared language, title and authors; broken if unreadable."""
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
            title = re.search(r"<dc:title[^>]*>([^<]*)</dc:title>", opf_text)
            return {"retail": not markers, "markers": markers, "lang": lang.group(1).casefold() if lang else None,
                    "broken": False, "title": html.unescape(title.group(1)).strip() if title else None,
                    "authors": [html.unescape(a).strip()
                                for a in re.findall(r"<dc:creator[^>]*>([^<]*)</dc:creator>", opf_text)]}
    except Exception:  # a download is untrusted: any failure to read it means it's broken
        return {"retail": False, "markers": ["unreadable"], "lang": None, "broken": True, "title": None,
                "authors": []}


def words(s):
    return re.findall(r"[^\W_]+", (s or "").casefold())


def authors(c):
    return c["authors"].split(" & ")


def main_title(s):
    return words(re.split(r"[:(]", s or "", maxsplit=1)[0])


SERIES_INDEX = re.compile(r"\d+|book|volume|vol|part")


def names_book(title, main):
    """Whether `main` runs through `title` word for word as a title, not as the series name before an index:
    "The Eternal Champion 3: The Dragon in the Sword" names another Book than "The Eternal Champion"."""
    seq = words(re.sub(r"\([^)]*\)|\[[^]]*\]", " ", title or ""))
    return bool(main) and any(seq[i:i + len(main)] == main
                              and not (i + len(main) < len(seq) and SERIES_INDEX.fullmatch(seq[i + len(main)]))
                              for i in range(len(seq) - len(main) + 1))


def same_book(c, info):
    """Whether a download is the planned Book: one title's main title (before any ":" or "(") runs through the
    other title, and, when the download names its authors, they share a surname.

    A search by Hardcover ID can return other Books of the same series, or a Book an omnibus collects.
    """
    if not (names_book(info["title"], main_title(c["title"])) or names_book(c["title"], main_title(info["title"]))):
        return False
    surnames = {words(a)[-1] for a in authors(c) if words(a)}
    return not info["authors"] or bool(surnames & {w for a in info["authors"] for w in words(a)})


FONT_MANGLING = {"http://www.idpf.org/2008/embedding": 1040, "http://ns.adobe.com/pdf/enc#RC": 1024}
FONT_SIGNATURES = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf", b"wOFF", b"wOF2")


def unmangle_fonts(z, opf_text):
    """The obfuscated fonts restored, by path, or None when encryption.xml holds anything else or no identifier
    unlocks a font. IDPF keys a font to the SHA-1 of the package identifier, Adobe to a urn:uuid identifier."""
    entries = re.findall(r'Algorithm="([^"]+)".*?URI="([^"]+)"', z.read("META-INF/encryption.xml").decode(
        "utf8", "replace"), re.S)
    ids = [re.sub(r"\s", "", v) for v in re.findall(r"<dc:identifier[^>]*>([^<]*)<", opf_text)]
    keys = {"http://www.idpf.org/2008/embedding": [hashlib.sha1(v.encode()).digest() for v in ids],
            "http://ns.adobe.com/pdf/enc#RC": [bytes.fromhex(m.replace("-", "")) for v in ids
                                               for m in re.findall(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-"
                                                                   r"[0-9a-fA-F]{12}", v)]}
    fonts = {}
    for algorithm, uri in entries:
        if algorithm not in FONT_MANGLING:
            return None
        name = urllib.parse.unquote(uri)
        if name not in z.namelist():
            continue
        data, n = z.read(name), FONT_MANGLING[algorithm]
        for key in keys[algorithm]:
            plain = bytes(b ^ key[i % len(key)] for i, b in enumerate(data[:n])) + data[n:]
            if plain.startswith(FONT_SIGNATURES):
                fonts[name] = plain
                break
        else:
            return None
    return fonts


def stamp(path, c):
    """Name the Book in the copy's OPF: its title, authors and Hardcover ID, and no ISBN or other identifier.

    The ingest gate then matches the copy to the Book by Hardcover ID (ADR 0016), not by the copy's own ISBN,
    Goodreads ID or title. The package's unique identifier gets a fresh UUID. Obfuscated fonts are keyed to an
    identifier, so they're restored first; when they can't be, the identifiers stay. The zip keeps mimetype
    first and stored.
    """
    path = Path(path)
    tmp = path.with_suffix(".stamping")
    try:
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            names = src.namelist()
            opf = next(n for n in names if n.endswith(".opf"))
            opf_text = src.read(opf).decode("utf8")
            fonts = unmangle_fonts(src, opf_text) if "META-INF/encryption.xml" in names else {}
            for item in src.infolist():
                if fonts is not None and item.filename == "META-INF/encryption.xml":
                    continue
                data = (stamp_opf(opf_text, c, keep_ids=fonts is None).encode("utf8") if item.filename == opf
                        else (fonts or {}).get(item.filename) or src.read(item.filename))
                dst.writestr(item, data, compress_type=zipfile.ZIP_STORED if item.filename == "mimetype"
                             else zipfile.ZIP_DEFLATED)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def stamp_opf(text, c, keep_ids):
    """The OPF naming only the Book; keep_ids keeps the package identifier and urn:uuid ones, as font keys."""
    uid = re.search(r"""<(?:\w+:)?package[^>]*\bunique-identifier=["']([^"']+)["']""", text)
    is_uid = uid and re.compile(rf"""\bid=["']{re.escape(uid.group(1))}["']""")
    dropped = []

    def keep_or_replace(m):
        if m.group(1) == "identifier" and is_uid and is_uid.search(m.group(0)):
            if keep_ids:
                return m.group(0)
            fresh = re.sub(r"""\s+opf:scheme=["'][^"']*["']""", "", m.group(0))
            return re.sub(r">[^<]*<", f">urn:uuid:{uuid.uuid4()}<", fresh, count=1)
        if m.group(1) == "identifier" and keep_ids and "urn:uuid:" in m.group(0):
            return m.group(0)
        dropped.extend(re.findall(r"""\bid=["']([^"']+)["']""", m.group(0)[:m.group(0).index(">")]))
        return ""

    text = re.sub(r"<dc:(title|creator|identifier)\b[^>]*?(?:/>|>[^<]*</dc:\1>)\s*", keep_or_replace, text)
    for i in dropped:
        text = re.sub(rf"""<meta\b[^>]*\brefines=["']#{re.escape(i)}["'][^>]*?(?:/>|>[^<]*</meta>)\s*""", "", text)
    ns = 'xmlns:dc="http://purl.org/dc/elements/1.1/"'
    named = (f"<dc:title {ns}>{html.escape(c['title'], quote=False)}</dc:title>"
             + "".join(f"<dc:creator {ns}>{html.escape(a, quote=False)}</dc:creator>" for a in authors(c))
             + f'<dc:identifier {ns} xmlns:opf="http://www.idpf.org/2007/opf" opf:scheme="hardcover-id">'
               f'{c["hardcover_id"]}</dc:identifier>')
    return re.sub(r"(?=</(?:\w+:)?metadata>)", lambda m: named, text, count=1)


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
    """Try each planned Book not yet in attempts_path; return "done", "limit", "quota" or "search failed".

    search(hardcover_id) -> Shelfmark releases; download(release, dest) -> fast downloads left (or None).
    Once a download has reported the fast downloads left, a Book starts only when all its tries fit above the
    reserve. A failed search stops the run and leaves the Book for the next one.
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
        try:
            found = search(c["hardcover_id"])
        except Exception:
            return "search failed"
        releases = [r for r in found if r.get("source") == "direct_download"]
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
                except Exception as e:  # the type only: a message may hold the URL, and the URL holds the key
                    dest.unlink(missing_ok=True)
                    row["tries"].append({"md5": md5, "error": str(e) if isinstance(e, DownloadError)
                                         else type(e).__name__})
                    continue
                info = inspect_epub(dest)
                if not info["broken"] and not same_book(c, info):
                    dest.unlink()
                    row["tries"].append({"md5": md5, "wrong book": f"{info['title']} / {', '.join(info['authors'])}"})
                    continue
                row["tries"].append({"md5": md5, "retail": info["retail"], "markers": info["markers"],
                                     "lang": info["lang"]})
                usable = not info["broken"] and english(info["lang"])
                # Any EPUB beats no EPUB, except over a PDF, whose layout may be the content
                fallback_ok = fallback is None and c["kind"] != "converted epub" and c.get("formats") != ["PDF"]
                if not (usable and (info["retail"] or fallback_ok)):
                    dest.unlink()
                    continue
                try:
                    stamp(dest, c)
                except Exception as e:  # a download is untrusted: one that won't rewrite is a failed try
                    dest.unlink()
                    row["tries"][-1]["error"] = f"stamp: {type(e).__name__}"
                    continue
                if info["retail"]:
                    put_in_ingest(dest, ingest, c["title"], md5, backlog, wait)
                    row["result"], row["ingested"] = "retail", md5
                    break
                fallback = staging / f"{md5}.keep"
                dest.rename(fallback)
                row["fallback"] = md5
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

def report(before, after, rows, still):
    """The availability report as Markdown, and the Books the after plan still lacks a Retail EPUB for."""
    tried = {r["id"]: r for r in rows}
    fallback = []
    for c in still:
        r = tried.get(c["id"], {})
        result = r.get("result", "not tried")
        fallback.append({"id": c["id"], "title": c["title"], "authors": c["authors"], "kind": c["kind"],
                         "result": "retail copy not merged" if result == "retail" else result,
                         "available": ", ".join(f"{k} {n}" for k, n in r.get("available", {}).items())})
    out = ["| Count | Before | After | Change |", "|---|---|---|---|"]
    out += [f"| {k} | {before[k]} | {after[k]} | {after[k] - before[k]:+d} |" for k in COUNTS]
    results = collections.Counter(r["result"] for r in rows)
    results.update(f["result"] for f in fallback if f["result"] in ("not tried", "retail copy not merged"))
    out += ["", "| Result | Books |", "|---|---|"]
    out += [f"| {k} | {n} |" for k, n in results.most_common()]
    formats = collections.Counter(k for c in still for k in tried.get(c["id"], {}).get("available", {}))
    out += ["", "Books still without a Retail EPUB, by the Formats Anna's Archive had (English releases only):", "",
            "| Format | Books |", "|---|---|"]
    out += [f"| {k} | {n} |" for k, n in formats.most_common()] or ["| none | 0 |"]
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
    f.add_argument("--pace", type=float, default=0, help="seconds between downloads")
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
        after, still = read_plan(a.after)
        rows = [json.loads(x) for x in Path(a.attempts).read_text().splitlines() if x.strip()]
        md, fallback = report(before, after, rows, still)
        sys.stdout.write(md)
        with open(a.fallback, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["id", "title", "authors", "kind", "result", "available"])
            w.writeheader()
            w.writerows(fallback)


if __name__ == "__main__":
    main()
