#!/usr/bin/env python3
"""Reduce the CWA Books Library to one Book per work and one Format per Book (ADR 0016).

Runs inside the calibre-web-automated container (see README.md), where the library, app.db, cwa.db,
calibredb and ebook-convert are. Commands:

  plan            read the library (read-only); print one JSON line per duplicate group, then per Book
  report          turn a plan into CSV for review (runs anywhere)
  epubcheck-list  the EPUBs (library-relative) whose groups need epubcheck to settle; feed epubcheck.sh
  apply           carry out a reviewed plan: merge groups, delete Books, convert and drop Formats, flag
  stats           counts to compare before and after (Books, Formats, Hardcover IDs, review, magic shelves)

Every choice between files follows ADR 0016's ranking, first difference wins, and records the step.
"""
import argparse
import csv
import functools
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from collections import Counter, defaultdict

LIBRARY = "/calibre-library"
APP_DB = "/config/app.db"
CWA_DB = "/config/cwa.db"
REVIEW_TAG = "review"
# Step 1: EPUB, then reflowable Formats calibre converts well to EPUB, then PDF (kept only when nothing
# else exists). Anything else (KFX and its MD/KFX-ZIP containers, ZIP, ORIGINAL_*, comics) is never kept.
FORMATS = ("EPUB", "AZW3", "MOBI", "AZW", "FB2", "LIT", "TXT", "RTF", "DOCX", "PDF")
KEPT_AS_IS = ("EPUB", "PDF")
COMIC_FORMATS = ("CBR", "CBZ", "CB7", "CBT")
COMIC_TAG = re.compile(r"\b(comics?|graphic novels?|manga)\b")
NOT_A_LANGUAGE = {"und", "mul", "zxx", "mis"}
# Markers calibre's conversion leaves in an EPUB; a Retail EPUB has none of them
CONVERTED = (("producer", re.compile(r'role="bkp"[^>]*>\s*calibre', re.I)),)
STEPS = ("1 format", "2 retail EPUB", "3 epubcheck", "4 table of contents", "5 larger file", "6 existing")
# app.db rows that follow a merged Book; a loser row that would duplicate a survivor row on these columns is dropped
HISTORY = {"downloads": ("user_id",), "archived_book": ("user_id",), "book_read_link": ("user_id",),
           "bookmark": ("user_id", "format"), "book_shelf_link": ("shelf",), "kobo_reading_state": ("user_id",),
           "kobo_synced_books": ("user_id",), "kobo_annotation_sync": ("annotation_id",),
           "hardcover_book_blacklist": (), "hardcover_match_queue": ()}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^\w\s]|_", " ", s).split())


def title_key(title):
    """The title without bracketed notes or a leading article. A subtitle stays: "Series: Book One" and
    "Series: Book Two" are different works; Hardcover IDs and CWA's cache group subtitle variants."""
    return re.sub(r"^(the|a|an) ", "", norm(re.sub(r"[(\[][^)\]]*[)\]]", " ", title or "")))


def titles_agree(a, b):
    """Equal titles, or one is the other's part before or after a colon ("Dune: The Battle of Corrin" and
    "The Battle of Corrin"). A wrong Hardcover ID or CWA's cache can group different works; those differ here."""
    def parts(t):
        return {title_key(t), *(title_key(p) for p in (t or "").split(":", 1))} - {""}
    return title_key(a) in parts(b) or title_key(b) in parts(a)


def author_key(name):
    """Surname and first initial, so "Weir, Andy", "Andy Weir" and "A. Weir" agree."""
    if "," in name:
        last, _, first = name.partition(",")
        name = f"{first} {last}"
    t = norm(name).split()
    return f"{t[-1]} {t[0][0]}" if len(t) > 1 else "".join(t)


# --- reading the library ---------------------------------------------------------------------

def inspect_epub(path):
    """Retail or converted, table-of-contents entries and declared language of an EPUB; broken if unreadable."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            opf = next((n for n in names if n.endswith(".opf")), None)
            opf_text = z.read(opf).decode("utf8", "replace") if opf else ""
            markers = [m for m, rx in CONVERTED if rx.search(opf_text)]
            if any(n.endswith("page_styles.css") for n in names):
                markers.append("page_styles.css")
            if any(re.search(r"index_split_\d+", n) for n in names):
                markers.append("index_split")
            pages = [n for n in names if n.endswith((".html", ".xhtml", ".htm"))]
            if pages and re.search(r'class="calibre\d*"', z.read(pages[len(pages) // 2]).decode("utf8", "replace")):
                markers.append("calibre classes")
            toc = 0
            for n in names:
                if n.endswith(".ncx"):
                    toc = max(toc, z.read(n).decode("utf8", "replace").count("<navPoint"))
                elif n.endswith((".xhtml", ".html")) and "nav" in n.rsplit("/", 1)[-1].lower():
                    nav = z.read(n).decode("utf8", "replace")
                    m = re.search(r'<nav[^>]*epub:type="toc".*?</nav>', nav, re.S)
                    if m:
                        toc = max(toc, m.group(0).count("<a "))
            lang = re.search(r"<dc:language[^>]*>\s*([^<\s]+)", opf_text)
            return {"retail": not markers, "markers": markers, "toc": toc,
                    "lang": lang.group(1).casefold() if lang else None, "broken": False}
    except (zipfile.BadZipFile, OSError, KeyError):
        return {"retail": False, "markers": ["unreadable"], "toc": 0, "lang": None, "broken": True}


def load(library, app_db, cwa_db):
    db = sqlite3.connect(f"file:{os.path.join(library, 'metadata.db')}?mode=ro", uri=True)
    books = {}
    for i, title, path, ts in db.execute("select id, title, path, timestamp from books order by id"):
        books[i] = {"id": i, "title": title, "path": path, "timestamp": ts or "", "authors": [], "langs": [],
                    "tags": [], "identifiers": {}, "files": []}
    for b, name in db.execute("select l.book, a.name from books_authors_link l join authors a on a.id=l.author "
                              "order by l.id"):
        books[b]["authors"].append(name)
    for b, code in db.execute("select l.book, g.lang_code from books_languages_link l join languages g "
                              "on g.id=l.lang_code order by l.item_order"):
        books[b]["langs"].append(code)
    for b, name in db.execute("select l.book, t.name from books_tags_link l join tags t on t.id=l.tag"):
        books[b]["tags"].append(name)
    for b, t, v in db.execute("select book, type, val from identifiers"):
        books[b]["identifiers"][t] = v
    for b, fmt, size, name in db.execute("select book, format, uncompressed_size, name from data order by format"):
        rel = f"{books[b]['path']}/{name}.{fmt.lower()}"
        f = {"book": b, "format": fmt, "path": rel, "size": size or 0, "timestamp": books[b]["timestamp"]}
        if fmt == "EPUB":
            f.update(inspect_epub(os.path.join(library, rel)))
        books[b]["files"].append(f)
    groups = []
    try:
        cwa = sqlite3.connect(f"file:{cwa_db}?mode=ro", uri=True)
        row = cwa.execute("select duplicate_groups_json from cwa_duplicate_cache").fetchone()
        groups = [g["book_ids"] for g in json.loads(row[0] or "[]")] if row else []
    except sqlite3.Error as e:
        log(f"CWA duplicate cache unreadable ({e}); grouping by title, author and Hardcover ID only")
    return {"library": library, "books": books, "cwa_groups": groups}


# --- ranking (ADR 0016) ----------------------------------------------------------------------

def _step_keys(f, checks):
    """One comparable value per step (higher is better), or None where the step can't judge this file."""
    epub = f["format"] == "EPUB"
    if epub and f.get("broken"):
        check = (1, 0)
    elif epub and f["path"] in checks:
        check = (checks[f["path"]]["fatal"], checks[f["path"]]["errors"])
    else:
        check = None
    return (
        -FORMATS.index(f["format"]) if f["format"] in FORMATS else -len(FORMATS),
        f["retail"] if epub else None,
        (-check[0], -check[1]) if check else None,
        f["toc"] > 1 if epub else None,
        f["size"],
        (_neg(f["timestamp"]), -f["book"]),
    )


def _neg(ts):
    """Older first: invert a timestamp string so that greater means earlier."""
    return tuple(-ord(c) for c in ts)


def compare(a, b, checks, missing=None):
    """(winner, step): the first ADR 0016 step on which a and b differ. A missing epubcheck result is
    noted in `missing` and that step skipped."""
    ka, kb = _step_keys(a, checks), _step_keys(b, checks)
    for i, step in enumerate(STEPS):
        if i == 2 and a["format"] == b["format"] == "EPUB" and (ka[2] is None or kb[2] is None):
            if missing is not None:
                missing.update(f["path"] for f, k in ((a, ka), (b, kb)) if k[2] is None)
            continue
        if ka[i] is None or kb[i] is None or ka[i] == kb[i]:
            continue
        return (a, step) if ka[i] > kb[i] else (b, step)
    return a, STEPS[-1]


def rank(files, checks, missing=None):
    cmp = lambda x, y: -1 if compare(x, y, checks, missing)[0] is x else 1  # noqa: E731
    return sorted(files, key=functools.cmp_to_key(cmp))


# --- planning --------------------------------------------------------------------------------

def deletion(book):
    """Why a Book leaves the Library (or None), and its flags. A comic tag alone is only a flag: the tags come from
    Hardcover genres, which put prose on comic shelves."""
    if not book["files"]:
        return "no files", []
    comic = sorted({f["format"] for f in book["files"]} & set(COMIC_FORMATS))
    if comic:
        return f"comic: {', '.join(comic)}", []
    tag = next((t for t in book["tags"] if COMIC_TAG.search(t.casefold())), None)
    flags = [f"comic? tagged {tag}"] if tag else []
    if book["langs"]:
        if not any(code.startswith("en") for code in book["langs"]):
            return f"language: {', '.join(book['langs'])}", []
        return None, flags
    lang = next((f["lang"] for f in book["files"] if f.get("lang")), None)
    if lang and lang.startswith("en"):
        return None, flags
    if lang and re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]+)*", lang) and lang.split("-")[0] not in NOT_A_LANGUAGE:
        return f"language: {lang} (from EPUB)", []
    return None, flags + ["language unknown"]


def group_books(books, cwa_groups):
    parent = {i: i for i in books}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(ids):
        ids = [i for i in ids if i in parent]
        for i in ids[1:]:
            parent[find(i)] = find(ids[0])

    by_key = defaultdict(list)
    for b in books.values():
        if b["authors"] and title_key(b["title"]):
            by_key[("title", title_key(b["title"]), author_key(b["authors"][0]))].append(b["id"])
        if b["identifiers"].get("hardcover-id"):
            by_key[("hardcover", b["identifiers"]["hardcover-id"])].append(b["id"])
    for ids in [*by_key.values(), *cwa_groups]:
        union(ids)
    out = defaultdict(list)
    for i in books:
        out[find(i)].append(i)
    return [sorted(g) for g in out.values() if len(g) > 1]


def format_choice(book, checks):
    usable = rank([f for f in book["files"] if f["format"] in FORMATS], checks)
    if not usable:
        return {"action": "review", "reason": "no usable Format: " + ", ".join(sorted(f["format"] for f in book["files"])),
                "keep": None, "convert_from": None, "remove": [], "decided_by": None}
    best = usable[0]
    others = [f for f in book["files"] if f is not best]
    decided = None
    if len(usable) > 1:
        decided = compare(best, usable[1], checks)[1]
    elif others:
        decided = STEPS[0]
    keep = best["format"] if best["format"] in KEPT_AS_IS else "EPUB"
    return {"action": "keep", "reason": None, "keep": keep,
            "convert_from": None if best["format"] in KEPT_AS_IS else best["format"],
            "remove": sorted(f["format"] for f in book["files"] if f["format"] != keep), "decided_by": decided}


def plan(lib, checks):
    books = lib["books"]
    out = {}
    flags = defaultdict(list)
    for b in books.values():
        reason, why = deletion(b)
        if reason:
            out[b["id"]] = {"action": "delete", "reason": reason}
        else:
            flags[b["id"]].extend(why)
    alive = {i: b for i, b in books.items() if i not in out}
    for ids in group_books(alive, [[i for i in g if i in alive] for g in lib["cwa_groups"]]):
        hc = sorted({books[i]["identifiers"]["hardcover-id"] for i in ids if books[i]["identifiers"].get("hardcover-id")})
        rec = {"kind": "group", "titles": {str(i): books[i]["title"] for i in ids},
               "formats": {str(i): sorted(f["format"] for f in books[i]["files"]) for i in ids}}
        why = None
        if len(hc) > 1:
            why = f"Hardcover IDs differ: {', '.join(hc)}"
        elif not all(titles_agree(books[a]["title"], books[b]["title"]) for a in ids for b in ids if a < b):
            why = "titles differ"
        if why:
            rec.update(status="review", reason=why, survivor=None, losers=ids,
                       decided_by=None, epubcheck_missing=[])
            for i in ids:
                flags[i].append("possible duplicate: " + ", ".join(str(j) for j in ids if j != i))
            yield rec
            continue
        missing = set()
        best = {i: rank(books[i]["files"], checks, missing)[0] for i in ids}
        order = rank(list(best.values()), checks, missing)
        survivor = order[0]["book"]
        _, step = compare(order[0], order[1], checks, missing)
        losers = sorted(i for i in ids if i != survivor)
        rec.update(status="merge", reason=None, survivor=survivor, losers=losers, decided_by=step,
                   steps={str(f["book"]): compare(order[0], f, checks)[1] for f in order[1:]},
                   epubcheck_missing=sorted(missing))
        for i in losers:
            out[i] = {"action": "merge", "reason": None, "into": survivor}
        yield rec
    for i, b in sorted(books.items()):
        rec = {"kind": "book", "book": i, "title": b["title"], "authors": b["authors"],
               "formats": sorted(f["format"] for f in b["files"])}
        if i in out:
            rec.update({"keep": None, "convert_from": None, "remove": [], "decided_by": None, "flags": [], **out[i]})
        else:
            rec.update(format_choice(b, checks))
            if rec["keep"] == "PDF":
                flags[i].append("PDF only: confirm layout")
            rec["flags"] = flags[i]
        yield rec


def epub_paths(records):
    return sorted({p for r in records if r["kind"] == "group" for p in r.get("epubcheck_missing", [])})


def load_checks(path):
    if not path:
        return {}
    with open(path) as fh:
        return {r["path"]: r for r in map(json.loads, filter(str.strip, fh))}


# --- applying --------------------------------------------------------------------------------

class Calibre:
    """The library writes apply makes, through calibredb and ebook-convert."""

    def __init__(self, library):
        self.library = library
        self.env = dict(os.environ, HOME=tempfile.gettempdir())

    def _run(self, *args):
        r = subprocess.run(list(args), capture_output=True, text=True, env=self.env)
        if r.returncode:
            log(f"{' '.join(args[:3])}: {(r.stderr or r.stdout).strip().splitlines()[-1:]}")
        return r.returncode == 0

    def _db(self, *args):
        return self._run("calibredb", "--with-library", self.library, *map(str, args))

    def remove_book(self, book):
        return self._db("remove", "--permanent", book)

    def remove_format(self, book, fmt):
        return self._db("remove_format", book, fmt)

    def set_identifiers(self, book, ids):
        return self._db("set_metadata", book, "--field", "identifiers:" + ",".join(f"{k}:{v}" for k, v in sorted(ids.items())))

    def add_tag(self, book, tag):
        db = sqlite3.connect(f"file:{os.path.join(self.library, 'metadata.db')}?mode=ro", uri=True)
        tags = [t for (t,) in db.execute("select t.name from books_tags_link l join tags t on t.id=l.tag "
                                         "where l.book=?", (book,))]
        if tag.casefold() in (t.casefold() for t in tags):
            return True
        return self._db("set_metadata", book, "--field", "tags:" + ",".join([*tags, tag]))

    def convert(self, book, source):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, f"{book}.epub")
            return self._run("ebook-convert", source, out) and self._db("add_format", book, out)


def move_history(app, loser, survivor):
    tables = {r[0] for r in app.execute("select name from sqlite_master where type='table'")}
    for table, key in HISTORY.items():
        if table not in tables:
            continue
        same = "".join(f' and s."{k}" is {table}."{k}"' for k in key)
        app.execute(f"delete from {table} where book_id=? and exists (select 1 from {table} s "
                    f"where s.book_id=?{same})", (loser, survivor))
        app.execute(f"update {table} set book_id=? where book_id=?", (survivor, loser))


def apply(records, library, app_db, calibre=None, only=None, skip=frozenset()):
    records = list(records)
    calibre = calibre or Calibre(library)
    meta = sqlite3.connect(f"file:{os.path.join(library, 'metadata.db')}?mode=ro", uri=True)
    app = sqlite3.connect(app_db)
    counts = Counter()

    def formats(i):
        return sorted(r[0] for r in meta.execute("select format from data where book=?", (i,)))

    def identifiers(i):
        return dict(meta.execute("select type, val from identifiers where book=?", (i,)).fetchall())

    def emit(**out):
        counts[out["result"]] += 1
        print(json.dumps(out, ensure_ascii=False), flush=True)

    skipped_groups = set()
    for g in (r for r in records if r["kind"] == "group" and r["status"] == "merge"):
        if (only and g["survivor"] not in only and not set(g["losers"]) & only) or {g["survivor"], *g["losers"]} & skip:
            continue
        moved = [i for i in [g["survivor"], *g["losers"]] if formats(i) != g["formats"][str(i)]]
        if moved:
            skipped_groups.add(g["survivor"])
            emit(group=g["survivor"], result="skipped", reason=f"changed since plan: {moved}")
            continue
        ids = identifiers(g["survivor"])
        merged = dict(ids)
        for i in g["losers"]:
            merged = {**identifiers(i), **merged}
            with app:
                move_history(app, i, g["survivor"])
        ok = merged == ids or calibre.set_identifiers(g["survivor"], merged)
        for i in g["losers"]:
            ok = calibre.remove_book(i) and ok
        emit(group=g["survivor"], merged=g["losers"], decided_by=g["decided_by"],
             result="applied" if ok else "failed")
    for r in (r for r in records if r["kind"] == "book" and r["action"] != "merge"):
        if (only and r["book"] not in only) or r["book"] in skip or r["book"] in skipped_groups:
            continue
        if formats(r["book"]) != r["formats"]:
            emit(book=r["book"], result="skipped", reason="changed since plan")
            continue
        ok, done = True, []
        if r["action"] == "delete":
            ok = calibre.remove_book(r["book"])
            done.append(f"deleted ({r['reason']})")
        else:
            if r["convert_from"]:
                src = next(f"{library}/{p[0]}/{p[1]}.{r['convert_from'].lower()}" for p in meta.execute(
                    "select b.path, d.name from data d join books b on b.id=d.book where d.book=? and d.format=?",
                    (r["book"], r["convert_from"])))
                ok = calibre.convert(r["book"], src)
                done.append(f"converted {r['convert_from']}")
            if ok:
                for fmt in r["remove"]:
                    ok = calibre.remove_format(r["book"], fmt) and ok
                done += [f"removed {f}" for f in r["remove"]]
            if r["flags"] or r["action"] == "review":
                ok = calibre.add_tag(r["book"], REVIEW_TAG) and ok
                done.append("flagged")
        if done:
            emit(book=r["book"], title=r["title"], done=done, decided_by=r.get("decided_by"),
                 result="applied" if ok else "failed")
    if counts["applied"]:
        with app:
            if app.execute("select 1 from sqlite_master where name='magic_shelf_cache'").fetchone():
                app.execute("delete from magic_shelf_cache")
    log(" ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to apply")
    return 1 if counts["failed"] else 0


# --- stats -----------------------------------------------------------------------------------

OPS = {"equal": lambda a, b: a == b, "not_equal": lambda a, b: a != b, "greater": lambda a, b: a > b,
       "greater_or_equal": lambda a, b: a >= b, "less": lambda a, b: a < b, "less_or_equal": lambda a, b: a <= b}


def shelf_books(meta, app, user, rules):
    """Book ids a magic shelf rule set selects, for the rule fields the system shelves use; None if unsupported."""
    every = {r[0] for r in meta.execute("select id from books")}
    sets = []
    for rule in rules.get("rules", []):
        if "rules" in rule:
            s = shelf_books(meta, app, user, rule)
        else:
            field, op, v = rule.get("field"), OPS.get(rule.get("operator")), rule.get("value")
            if field in ("timestamp", "pubdate") and op:
                s = {i for i, d in meta.execute(f"select id, substr({field},1,10) from books") if d and op(d, str(v))}
            elif field == "rating" and op:
                s = {i for i, x in meta.execute("select l.book, r.rating from books_ratings_link l join ratings r "
                                                "on r.id=l.rating") if x is not None and op(x, int(v))}
            elif field == "tags" and rule.get("operator") in ("contains", "equal", "in"):
                want = {str(t).casefold() for t in (v if isinstance(v, list) else [v])}
                s = {i for i, t in meta.execute("select l.book, t.name from books_tags_link l join tags t "
                                                "on t.id=l.tag") if t.casefold() in want}
            elif field == "read_status" and rule.get("operator") == "equal":
                status = dict(app.execute("select book_id, read_status from book_read_link where user_id=?", (user,)))
                s = {i for i in every if status.get(i, 0) == int(v)}
            else:
                s = None
        if s is None:
            return None
        sets.append(s)
    if not sets:
        return every
    return set.union(*sets) if rules.get("condition") == "OR" else set.intersection(*sets)


def stats(library, app_db):
    meta = sqlite3.connect(f"file:{os.path.join(library, 'metadata.db')}?mode=ro", uri=True)
    one = lambda q: meta.execute(q).fetchone()[0]  # noqa: E731
    out = {
        "books": one("select count(*) from books"),
        "formats": dict(meta.execute("select format, count(*) from data group by format order by format")),
        "multi_format_books": one("select count(*) from (select book from data group by book having count(*) > 1)"),
        "books_without_epub": one("select count(*) from books where id not in (select book from data where format='EPUB')"),
        "hardcover_ids": one("select count(distinct book) from identifiers where type='hardcover-id'"),
        "review": one(f"select count(*) from books_tags_link l join tags t on t.id=l.tag "
                      f"where lower(t.name)='{REVIEW_TAG}'"),
        "magic_shelves": {},
    }
    if os.path.exists(app_db):
        app = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True)
        for user, name, rules in app.execute("select user_id, name, rules from magic_shelf order by user_id, name"):
            s = shelf_books(meta, app, user, json.loads(rules or "{}"))
            out["magic_shelves"][f"{user}/{name}"] = None if s is None else len(s)
    return out


# --- report ----------------------------------------------------------------------------------

def report(records, out):
    w = csv.writer(out)
    w.writerow(["kind", "book", "action", "reason", "decided_by", "title", "authors", "formats", "keep",
                "convert_from", "remove", "flags", "into_or_losers"])
    counts = Counter()
    for r in records:
        if r["kind"] == "group":
            counts[f"group {r['status']}"] += 1
            w.writerow(["group", r["survivor"] or "", r["status"], r.get("reason") or "", r.get("decided_by") or "",
                        " | ".join(r["titles"].values()), "", json.dumps(r["formats"]), "", "", "", "",
                        ", ".join(map(str, r["losers"]))])
            continue
        counts[r["action"]] += 1
        if r.get("convert_from"):
            counts["convert"] += 1
        counts.update(f"flag {f.split(':')[0]}" for f in r["flags"])
        w.writerow(["book", r["book"], r["action"], r.get("reason") or "", r.get("decided_by") or "", r["title"],
                    ", ".join(r["authors"]), ", ".join(r["formats"]), r.get("keep") or "", r.get("convert_from") or "",
                    ", ".join(r["remove"]), "; ".join(r["flags"]), r.get("into") or ""])
    for k, n in sorted(counts.items()):
        log(f"{k:40} {n}")


# --- CLI -------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="print the plan, one JSON line per group and per Book")
    p.add_argument("--epubcheck", help="JSON lines {path, fatal, errors} from epubcheck.sh")
    a = sub.add_parser("apply", help="carry out a reviewed plan (stdin)")
    a.add_argument("--only", help="comma-separated Book ids (a group's survivor or loser selects the group)")
    a.add_argument("--skip", help="comma-separated Book ids the Operator kept out (a member skips its whole group)")
    s = sub.add_parser("stats", help="counts to compare before and after, as JSON")
    for x in (p, a, s):
        x.add_argument("--library", default=LIBRARY)
        x.add_argument("--app-db", default=APP_DB)
    p.add_argument("--cwa-db", default=CWA_DB)
    sub.add_parser("report", help="plan (stdin) to CSV (stdout)")
    sub.add_parser("epubcheck-list", help="plan (stdin) to the EPUB paths epubcheck.sh should check")
    args = ap.parse_args(argv)

    read = lambda: [json.loads(line) for line in sys.stdin if line.strip()]  # noqa: E731
    if args.cmd == "report":
        report(read(), sys.stdout)
    elif args.cmd == "epubcheck-list":
        print("\n".join(epub_paths(read())))
    elif args.cmd == "stats":
        print(json.dumps(stats(args.library, args.app_db), indent=2))
    elif args.cmd == "apply":
        only = {int(x) for x in args.only.split(",")} if args.only else None
        skip = {int(x) for x in args.skip.split(",")} if args.skip else frozenset()
        return apply(read(), args.library, args.app_db, only=only, skip=skip)
    else:
        lib = load(args.library, args.app_db, args.cwa_db)
        log(f"planning {len(lib['books'])} Books")
        for rec in plan(lib, load_checks(args.epubcheck)):
            print(json.dumps(rec, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
