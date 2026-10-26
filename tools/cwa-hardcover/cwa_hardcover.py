#!/usr/bin/env python3
"""Match the CWA library to Hardcover and write each match's metadata.

Runs inside the calibre-web-automated container (see README.md), where the library, calibredb and
HARDCOVER_TOKEN are. Three commands:

  plan    read the library (read-only), match each book, print one JSON line per book: its match,
          and every field's current and new value
  report  turn a plan into CSV for review (runs anywhere)
  apply   write the matched books of a reviewed plan with calibredb; skip a book whose fields
          changed since the plan

Matching, strongest first: the book's ISBN names a Hardcover edition; its Goodreads ID maps to a
Hardcover book; or a title search (as the book editor does) scores each result on title and
author, and the best one passes the threshold. Every match must share an author with the book.
"""
import argparse
import csv
import difflib
import html
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter

API = "https://api.hardcover.app/v1/graphql"
LIBRARY = "/calibre-library"
THRESHOLD = 0.85
REQUEST_INTERVAL = 1.1  # Hardcover allows 60 requests a minute per token
LOOKUP_BATCH = 100
DETAIL_BATCH = 25
MAX_TAGS = 10
MIN_TAG_VOTES = 2  # most Hardcover genre tags carry a single reader's vote
MIN_RATINGS = 5
GOODREADS_PLATFORM = 1
MAX_COVER = (1650, 2200)  # calibre's maximum_cover_size: calibredb shrinks larger covers to fit
# Study aids and summaries that title search ranks above the book itself
JUNK = re.compile(r"\b(summary|summaries|analysis|study guide|sparknotes|cliffsnotes|workbook|trivia|quiz)\b")
FIELDS = ("identifiers", "series", "description", "publisher", "pubdate", "rating", "tags", "cover")

EDITION = "id isbn_13 isbn_10 release_date publisher{name} image{url width height}"
SEARCH_QUERY = 'query($q:String!){search(query:$q,query_type:"Book",per_page:10){results}}'
ISBN_QUERY = ("query($i13:[String!],$i10:[String!]){editions(where:{_or:[{isbn_13:{_in:$i13}},"
              "{isbn_10:{_in:$i10}}]}){book_id " + EDITION + "}}")
GOODREADS_QUERY = ("query($e:[String!]){book_mappings(where:{platform_id:{_eq:%d},external_id:{_in:$e}})"
                   "{book_id external_id}}" % GOODREADS_PLATFORM)
DETAIL_QUERY = ("query($ids:[Int!]){books(where:{id:{_in:$ids}}){id slug title description rating ratings_count "
                "release_date cached_tags cached_contributors book_series{position featured series{name}} "
                "image{url width height} default_ebook_edition{" + EDITION + "} "
                "default_physical_edition{" + EDITION + "}}}")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --- text matching ---------------------------------------------------------------------------

def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^\w\s]|_", " ", s).split())


def title_variants(title):
    """The title, its main title (before a colon or bracketed series note), each without a leading article."""
    out = []
    for t in (title, re.split(r"[:(\[]", title or "")[0]):
        n = norm(t)
        for v in (n, re.sub(r"^(the|a|an) ", "", n)):
            if v and v not in out:
                out.append(v)
    return out


def title_similarity(ours, theirs):
    return max((difflib.SequenceMatcher(None, a, b).ratio()
                for a in title_variants(ours) for t in theirs for b in title_variants(t)), default=0.0)


def author_tokens(name):
    if "," in name:  # "Last, First" as Calibre's author sort writes it
        last, _, first = name.partition(",")
        name = f"{first} {last}"
    return norm(name).split()


def authors_match(ours, theirs):
    """Some author on each side shares a surname, and their first names or initials agree."""
    for a in ours:
        ta = author_tokens(a)
        for b in theirs:
            tb = author_tokens(b)
            if not ta or not tb:
                continue
            if "".join(ta) == "".join(tb):
                return True
            if ta[-1] == tb[-1] and (len(ta) == 1 or len(tb) == 1 or ta[0][0] == tb[0][0]):
                return True
    return False


def score(book, cand):
    """0..1: title similarity weighted 0.6 plus 0.4 for a shared author. A shared ISBN scores 1."""
    if not authors_match(book["authors"], cand["authors"]):
        return 0.0
    if set(book["isbns"]) & set(cand.get("isbns", ())):
        return 1.0
    s = 0.6 * title_similarity(book["title"], [cand["title"], *cand.get("alternative_titles", [])]) + 0.4
    if JUNK.search(norm(cand["title"])) and not JUNK.search(norm(book["title"])):
        s -= 0.5
    if cand.get("compilation"):
        s -= 0.1
    return round(max(s, 0.0), 3)


def choose(book, cands, threshold=THRESHOLD):
    """The best-scoring candidate (ties go to the one more Hardcover users have), and whether it passes."""
    ranked = sorted(((score(book, c), c.get("users_count") or 0, c) for c in cands),
                    key=lambda r: (r[0], r[1]), reverse=True)
    if not ranked:
        return None, 0.0, False
    best, _, cand = ranked[0]
    return cand, best, best >= threshold


# --- identifiers -----------------------------------------------------------------------------

def isbn13(raw):
    d = re.sub(r"[^0-9Xx]", "", raw or "").upper()
    if len(d) == 13 and d.isdigit():
        return d
    if len(d) == 10:
        core = "978" + d[:9]
        check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
        return core + str(check)
    return None


def isbn10(i13):
    if not i13 or not i13.startswith("978"):
        return None
    core = i13[3:12]
    check = (11 - sum(int(c) * (10 - i) for i, c in enumerate(core)) % 11) % 11
    return core + ("X" if check == 10 else str(check))


def goodreads_id(raw):
    m = re.match(r"\d+", (raw or "").strip())
    return m.group(0) if m else None


# --- library ---------------------------------------------------------------------------------

def connect(library, write=False):
    path = os.path.join(library, "metadata.db")
    return sqlite3.connect(path if write else f"file:{path}?mode=ro", uri=not write)


def current(db, library, book_id):
    """A book's matching inputs and the current value of every field the plan may change."""
    one = lambda q: db.execute(q, (book_id,)).fetchone()  # noqa: E731
    many = lambda q: [r[0] for r in db.execute(q, (book_id,))]  # noqa: E731
    row = one("select title, path, has_cover, series_index, pubdate from books where id=?")
    if not row:
        return None
    title, path, has_cover, series_index, pubdate = row
    identifiers = dict(db.execute("select type, val from identifiers where book=?", (book_id,)).fetchall())
    series = one("select s.name from books_series_link l join series s on s.id=l.series where l.book=?")
    comment = one("select text from comments where book=?")
    publisher = one("select p.name from books_publishers_link l join publishers p on p.id=l.publisher where l.book=?")
    rating = one("select r.rating from books_ratings_link l join ratings r on r.id=l.rating where l.book=?")
    cover = os.path.join(library, path, "cover.jpg")
    return {
        "id": book_id,
        "title": title,
        "authors": many("select a.name from books_authors_link l join authors a on a.id=l.author "
                        "where l.book=? order by l.id"),
        "isbns": sorted({i for i in (isbn13(identifiers.get("isbn")),) if i}),
        "goodreads": goodreads_id(identifiers.get("goodreads")),
        "fields": {
            "identifiers": identifiers,
            "series": [series[0], float(series_index or 1)] if series else None,
            "description": comment[0] if comment else None,
            "publisher": publisher[0] if publisher else None,
            "pubdate": None if not pubdate or pubdate.startswith("0101") else pubdate[:10],
            "rating": rating[0] if rating and rating[0] else None,
            "tags": sorted(many("select t.name from books_tags_link l join tags t on t.id=l.tag where l.book=?")),
            "cover": {"area": image_area(cover)} if has_cover else None,
        },
    }


def image_area(path):
    try:
        from PIL import Image  # in the CWA image; absent elsewhere
        with Image.open(path) as im:
            return im.size[0] * im.size[1]
    except Exception:
        return None


# --- Hardcover -------------------------------------------------------------------------------

class Hardcover:
    def __init__(self, token, interval=REQUEST_INTERVAL):
        self.token = token.replace("Bearer ", "").strip()
        self.interval = interval
        self.last = 0.0
        self.calls = 0

    def gql(self, query, variables):
        for attempt in range(6):
            wait = self.last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            req = urllib.request.Request(API, json.dumps({"query": query, "variables": variables}).encode(), {
                "Content-Type": "application/json", "User-Agent": "cwa-hardcover",
                "authorization": f"Bearer {self.token}"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt * 5)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 ** attempt * 5)
                continue
            if body.get("errors"):
                raise RuntimeError(f"Hardcover: {body['errors']}")
            return body["data"]
        raise RuntimeError("Hardcover: gave up after 6 attempts")

    def search(self, query):
        res = self.gql(SEARCH_QUERY, {"q": query})["search"]["results"]
        res = json.loads(res) if isinstance(res, str) else res
        out = []
        for hit in (res or {}).get("hits", []):
            d = hit.get("document") or {}
            out.append({"id": int(d["id"]), "title": d.get("title") or "", "authors": d.get("author_names") or [],
                        "alternative_titles": d.get("alternative_titles") or [],
                        "isbns": sorted({i for i in map(isbn13, d.get("isbns") or []) if i}),
                        "users_count": d.get("users_count") or 0, "compilation": bool(d.get("compilation"))})
        return out

    def editions_by_isbn(self, i13s):
        out = {}
        for chunk in chunks(sorted(i13s), LOOKUP_BATCH):
            i10 = [x for x in map(isbn10, chunk) if x]
            for e in self.gql(ISBN_QUERY, {"i13": chunk, "i10": i10})["editions"]:
                for raw in (e.get("isbn_13"), e.get("isbn_10")):
                    if isbn13(raw):
                        out[isbn13(raw)] = e
        return out

    def books_by_goodreads(self, ids):
        out = {}
        for chunk in chunks(sorted(ids), LOOKUP_BATCH):
            for m in self.gql(GOODREADS_QUERY, {"e": chunk})["book_mappings"]:
                out.setdefault(m["external_id"], m["book_id"])
        return out

    def details(self, ids):
        out = {}
        for chunk in chunks(sorted(ids), DETAIL_BATCH):
            for b in self.gql(DETAIL_QUERY, {"ids": chunk})["books"]:
                out[b["id"]] = b
        return out


def chunks(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def contributors(detail):
    return [c["author"]["name"] for c in detail.get("cached_contributors") or []
            if (c.get("author") or {}).get("name") and c.get("contribution") in (None, "", "Author")]


# --- planning --------------------------------------------------------------------------------

def description_html(text):
    paras = [p.strip() for p in re.split(r"\n\s*\n|\r?\n", text or "") if p.strip()]
    return "".join(f"<p>{html.escape(p)}</p>" for p in paras) or None


def pick_series(old, book_series):
    """Hardcover's featured series, only for a book without one: Hardcover's series names and
    positions often disagree with a library's own (omnibuses, novellas, reading vs publication order)."""
    entries = [s for s in book_series or [] if (s.get("series") or {}).get("name")]
    if old or not entries:
        return None
    s = next((s for s in entries if s.get("featured")), entries[0])
    return [s["series"]["name"], float(s["position"]) if s.get("position") is not None else 1.0]


def genres(detail):
    tags = (detail.get("cached_tags") or {}).get("Genre") or []
    out, seen = [], set()
    for t in tags:
        if not isinstance(t, dict) or (t.get("count") or 0) < MIN_TAG_VOTES:
            continue
        name = (t.get("tag") or "").strip()
        if name and "," not in name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return sorted(out[:MAX_TAGS])


def best_image(*images):
    found = [i for i in images if i and i.get("url") and i.get("width") and i.get("height")]
    return max(found, key=lambda i: i["width"] * i["height"], default=None)


def stored_area(width, height):
    """The area calibre keeps of a width x height cover."""
    scale = min(1.0, MAX_COVER[0] / width, MAX_COVER[1] / height)
    return int(width * scale) * int(height * scale)


def pick_pubdate(old, release_date):
    """Hardcover's original release date, unless the book already has a date in that year: Hardcover
    often stores a year alone as January 1."""
    new = (release_date or "")[:10] or None
    if new and old and old[:4] == new[:4]:
        return None
    return new


def search_queries(book):
    main = re.split(r"[:(\[]", book["title"])[0].strip()
    qs = [book["title"], main] + ([f"{main} {book['authors'][0]}"] if book["authors"] else [])
    return [q for q in dict.fromkeys(qs) if q]


def new_fields(book, detail, edition):
    """The value each field would take. None means leave the field as it is: Hardcover never clears one."""
    old = book["fields"]
    ebook, physical = detail.get("default_ebook_edition"), detail.get("default_physical_edition")
    edition = edition or ebook or physical or {}
    ids = dict(old["identifiers"])
    ids["hardcover-id"] = str(detail["id"])
    if detail.get("slug"):
        ids["hardcover-slug"] = detail["slug"]
    if edition.get("id"):
        ids["hardcover-edition"] = str(edition["id"])
    if not book["isbns"] and isbn13(edition.get("isbn_13") or edition.get("isbn_10")):
        ids["isbn"] = isbn13(edition.get("isbn_13") or edition.get("isbn_10"))
    rating = None
    if detail.get("rating") and (detail.get("ratings_count") or 0) >= MIN_RATINGS:
        rating = int(round(detail["rating"] * 2))
    publisher = next((e["publisher"]["name"].strip() for e in (edition, ebook, physical)
                      if e and ((e.get("publisher") or {}).get("name") or "").strip()), None)
    image = best_image(detail.get("image"), edition.get("image"), (ebook or {}).get("image"),
                       (physical or {}).get("image"))
    cover = None
    if image:
        area = stored_area(image["width"], image["height"])
        # A 1% margin absorbs calibre's rounding when it scales a cover down
        if old["cover"] is None or (old["cover"].get("area") is not None and area > old["cover"]["area"] * 1.01):
            cover = {"url": image["url"], "area": area}
    return {
        "identifiers": ids,
        "series": pick_series(old["series"], detail.get("book_series")),
        "description": description_html(detail.get("description")),
        "publisher": publisher,
        "pubdate": pick_pubdate(old["pubdate"], detail.get("release_date")),
        "rating": rating,
        "tags": genres(detail) or None,
        "cover": cover,
    }


def changes(book, new):
    return {f: {"old": book["fields"][f], "new": new[f]} for f in FIELDS
            if new[f] is not None and new[f] != book["fields"][f]}


def plan(books, hc, threshold=THRESHOLD):
    by_isbn = hc.editions_by_isbn({i for b in books for i in b["isbns"]})
    log(f"ISBN: {sum(1 for b in books if any(i in by_isbn for i in b['isbns']))} books")
    by_gr = hc.books_by_goodreads({b["goodreads"] for b in books if b["goodreads"]})
    log(f"Goodreads: {sum(1 for b in books if b['goodreads'] in by_gr)} books")

    picks = {}
    for n, b in enumerate(books, 1):
        isbn_books = {by_isbn[i]["book_id"] for i in b["isbns"] if i in by_isbn}
        gr_book = by_gr.get(b["goodreads"])
        edition = next((by_isbn[i] for i in b["isbns"] if i in by_isbn), None)
        if len(isbn_books) > 1:
            picks[b["id"]] = {"status": "review", "reason": "ISBNs name different Hardcover books"}
        elif isbn_books:
            hid = next(iter(isbn_books))
            note = " (Goodreads ID names another book)" if gr_book and gr_book != hid else ""
            picks[b["id"]] = {"via": "isbn", "hid": hid, "edition": edition, "note": note}
        elif gr_book:
            picks[b["id"]] = {"via": "goodreads", "hid": gr_book}
        else:
            best = (None, 0.0, False)
            for q in search_queries(b):
                cand, s, ok = choose(b, hc.search(q), threshold)
                if cand and s > best[1]:
                    best = (cand, s, ok)
                if ok:
                    break
            cand, s, ok = best
            if ok:
                picks[b["id"]] = {"via": "search", "hid": cand["id"], "score": s}
            else:
                picks[b["id"]] = {"status": "none", "score": s, "reason": "no search result passed the threshold",
                                  "near": cand}
        if n % 25 == 0:
            log(f"matched {n}/{len(books)} ({hc.calls} requests)")

    details = hc.details({p["hid"] for p in picks.values() if "hid" in p})
    for b in books:
        p = picks[b["id"]]
        rec = {"book": b["id"], "title": b["title"], "authors": b["authors"], "status": p.get("status"),
               "via": p.get("via"), "score": p.get("score"), "reason": p.get("reason")}
        near = p.get("near")
        if near:
            rec["hardcover"] = {"id": near["id"], "title": near["title"], "authors": near["authors"]}
        d = details.get(p.get("hid"))
        if "hid" in p and not d:
            rec.update(status="review", reason="Hardcover book not found")
        elif d:
            authors = contributors(d)
            rec["hardcover"] = {"id": d["id"], "title": d["title"], "authors": authors,
                                "url": f"https://hardcover.app/books/{d.get('slug') or d['id']}"}
            sim = round(title_similarity(b["title"], [d["title"]]), 3)
            if rec["score"] is None:
                rec["score"] = sim
            if not authors_match(b["authors"], authors):
                rec.update(status="review", reason="no shared author")
            elif p["via"] != "search" and sim < 0.5:
                rec.update(status="review", reason=f"title differs ({sim})")
            else:
                rec.update(status="match", reason=(p.get("note") or "").strip() or None)
            rec["changes"] = changes(b, new_fields(b, d, p.get("edition")))
        yield rec


# --- applying --------------------------------------------------------------------------------

def calibredb_fields(chg, cover_path=None):
    args = []
    for f, c in chg.items():
        v = c["new"]
        if f == "identifiers":
            args.append("identifiers:" + ",".join(f"{k}:{val}" for k, val in sorted(v.items())))
        elif f == "series":
            args += [f"series:{v[0]}", f"series_index:{v[1]:g}"]
        elif f == "description":
            args.append(f"comments:{v}")
        elif f == "publisher":
            args.append(f"publisher:{v}")
        elif f == "pubdate":
            args.append(f"pubdate:{v}")
        elif f == "rating":
            args.append(f"rating:{v / 2:g}")
        elif f == "tags":
            args.append("tags:" + ",".join(v))
        elif f == "cover" and cover_path:
            args.append(f"cover:{cover_path}")
    return [x for a in args for x in ("--field", a)]


def drifted(rec, now):
    return [f for f, c in rec["changes"].items() if now["fields"][f] != c["old"]]


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "cwa-hardcover"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        out.write(resp.read())
    return image_area(dest) is not None


def apply(records, library, only=None):
    db = connect(library)
    env = dict(os.environ, HOME=tempfile.gettempdir())
    counts = Counter()
    with tempfile.TemporaryDirectory() as tmp:
        for rec in records:
            if rec["status"] != "match" or not rec.get("changes") or (only and rec["book"] not in only):
                continue
            now = current(db, library, rec["book"])
            out = {"book": rec["book"], "title": rec["title"]}
            moved = drifted(rec, now) if now else ["book"]
            if moved:
                out.update(result="skipped", reason="changed since plan: " + ",".join(moved))
            else:
                cover = None
                if "cover" in rec["changes"]:
                    cover = os.path.join(tmp, f"{rec['book']}.img")
                    try:
                        if not download(rec["changes"]["cover"]["new"]["url"], cover):
                            cover = None
                    except Exception:
                        cover = None
                fields = calibredb_fields(rec["changes"], cover)
                r = subprocess.run(["calibredb", "--with-library", library, "set_metadata", str(rec["book"]), *fields],
                                   capture_output=True, text=True, env=env)
                if r.returncode:
                    out.update(result="failed", reason=(r.stderr or r.stdout).strip().splitlines()[-1:])
                else:
                    out.update(result="applied", fields=sorted(f for f in rec["changes"] if f != "cover" or cover))
            counts[out["result"]] += 1
            print(json.dumps(out), flush=True)
    log(" ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to apply")
    return 1 if counts["failed"] else 0


# --- report ----------------------------------------------------------------------------------

def show(v):
    if v is None:
        return ""
    if isinstance(v, dict) and "area" in v:
        return f"{v.get('area')} px" + (f" {v['url']}" if v.get("url") else "")
    if isinstance(v, list):
        return ", ".join(map(str, v))
    if isinstance(v, dict):
        return ", ".join(f"{k}:{x}" for k, x in sorted(v.items()))
    return re.sub(r"<[^>]+>", " ", str(v)).strip()[:200]


def report(records, out):
    w = csv.writer(out)
    w.writerow(["book", "status", "via", "score", "reason", "title", "authors", "hardcover_title",
                "hardcover_authors", "url", *[f"{f}_{s}" for f in FIELDS for s in ("old", "new")]])
    status, removed = Counter(), Counter()
    for r in records:
        status[(r["status"], r.get("via"))] += 1
        hc, chg = r.get("hardcover") or {}, r.get("changes") or {}
        if r["status"] == "match" and "tags" in chg:
            removed.update(set(chg["tags"]["old"]) - set(chg["tags"]["new"]))
        w.writerow([r["book"], r["status"], r.get("via") or "", r.get("score") or "", r.get("reason") or "",
                    r["title"], ", ".join(r["authors"]), hc.get("title", ""), ", ".join(hc.get("authors", [])),
                    hc.get("url", ""), *[show(chg[f][s]) if f in chg else "" for f in FIELDS for s in ("old", "new")]])
    for (s, via), n in sorted(status.items(), key=lambda x: (x[0][0], x[0][1] or "")):
        log(f"{s:7} {via or '':10} {n}")
    if removed:
        log("tags removed most: " + ", ".join(f"{t} ({n})" for t, n in removed.most_common(10)))


# --- CLI -------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="match books; print one JSON line per book")
    p.add_argument("--library", default=LIBRARY)
    p.add_argument("--sample", type=int, help="this many random books")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--ids", help="comma-separated book ids")
    p.add_argument("--threshold", type=float, default=THRESHOLD)
    a = sub.add_parser("apply", help="write a reviewed plan (stdin) with calibredb")
    a.add_argument("--library", default=LIBRARY)
    a.add_argument("--only", help="comma-separated book ids")
    sub.add_parser("report", help="plan (stdin) to CSV (stdout)")
    args = ap.parse_args(argv)

    if args.cmd == "report":
        report((json.loads(line) for line in sys.stdin if line.strip()), sys.stdout)
        return 0
    if args.cmd == "apply":
        only = {int(x) for x in args.only.split(",")} if args.only else None
        return apply((json.loads(line) for line in sys.stdin if line.strip()), args.library, only)

    token = os.environ.get("HARDCOVER_TOKEN")
    if not token:
        log("HARDCOVER_TOKEN is not set")
        return 2
    db = connect(args.library)
    ids = [r[0] for r in db.execute("select id from books order by id")]
    if args.ids:
        ids = [int(x) for x in args.ids.split(",")]
    elif args.sample:
        ids = sorted(random.Random(args.seed).sample(ids, min(args.sample, len(ids))))
    books = [b for b in (current(db, args.library, i) for i in ids) if b]
    log(f"planning {len(books)} books")
    for rec in plan(books, Hardcover(token), args.threshold):
        print(json.dumps(rec, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
