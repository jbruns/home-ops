#!/usr/bin/env python3
"""Match the CWA library to Hardcover and write each match's metadata.

Runs inside the calibre-web-automated container (see README.md), where the library, calibredb,
app.db and HARDCOVER_TOKEN are. Four commands:

  plan    read the library and app.db (read-only), match each book, print one JSON line per book (its
          match, flags, and every field's current and new value) and one per magic shelf to change
  report  turn a plan into CSV for review (runs anywhere)
  tags    count a plan's Hardcover genres and what the controlled tag list maps each to (runs anywhere)
  apply   write a reviewed plan with calibredb and to app.db; skip a book whose fields changed since
          the plan

Matching, strongest first: the book's ISBN names a Hardcover edition; its Goodreads ID maps to a
Hardcover book; its hardcover-id identifier names one; or a title search (as the book editor does)
scores each result on title and author, and the best one passes the threshold, trying the title and
author swapped if neither way passes. Every match must share an author with the book, and a match
not found by search must have a close title.
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
import uuid
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

API = "https://api.hardcover.app/v1/graphql"
LIBRARY = "/calibre-library"
APP_DB = "/config/app.db"
HERE = os.path.dirname(os.path.abspath(__file__))
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
FIELDS = ("title", "authors", "identifiers", "series", "description", "publisher", "pubdate", "rating", "tags",
          "cover")
REVIEW_TAG = "review"  # every flagged book carries it; only the Operator removes it
REVIEW_SHELF = "Review"
REVIEW_RULES = {"condition": "AND", "rules": [{"id": "tag", "field": "tag", "type": "string", "input": "text",
                                               "operator": "equal", "value": REVIEW_TAG}]}
SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of", "on", "or", "the", "to", "with"}
NUMBER_WORDS = {
    **{w: i for i, w in enumerate("one two three four five six seven eight nine ten eleven twelve".split(), 1)},
    **{w: i for i, w in enumerate("first second third fourth fifth sixth seventh eighth ninth tenth".split(), 1)},
    **{w: i for i, w in enumerate("ii iii iv".split(), 2)}, **{w: i for i, w in enumerate("vi vii viii ix".split(), 6)},
}
MAX_OMNIBUS = 200  # a wider range in series details is a typo, not an omnibus

EDITION = "id isbn_13 isbn_10 release_date publisher{name} image{url width height}"
SEARCH_QUERY = 'query($q:String!){search(query:$q,query_type:"Book",per_page:10){results}}'
ISBN_QUERY = ("query($i13:[String!],$i10:[String!]){editions(where:{_or:[{isbn_13:{_in:$i13}},"
              "{isbn_10:{_in:$i10}}]}){book_id " + EDITION + "}}")
GOODREADS_QUERY = ("query($e:[String!]){book_mappings(where:{platform_id:{_eq:%d},external_id:{_in:$e}})"
                   "{book_id external_id}}" % GOODREADS_PLATFORM)
DETAIL_QUERY = ("query($ids:[Int!]){books(where:{id:{_in:$ids}}){id slug title description rating ratings_count "
                "release_date subtitle compilation cached_tags cached_contributors "
                "book_series{position details featured series{id name}} "
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


def main_title(title):
    return re.sub(r"^(the|a|an) ", "", norm(re.split(r"[:(\[]", title or "")[0]))


def numbers(title):
    return {int(t) if t.isdigit() else NUMBER_WORDS[t] for t in norm(title).split() if t.isdigit() or t in NUMBER_WORDS}


PART = re.compile(r"\b(?:part|vol|volume|book)\s+(\w+)")


def numbers_conflict(ours, theirs):
    """The titles name different parts or volumes: both are numbered and neither's numbers include the
    other's, or Hardcover's names a part or volume whose number ours lacks."""
    a, b = numbers(ours), numbers(theirs)
    parts = numbers(" ".join(PART.findall(norm(theirs))))
    return bool(a and b and not a <= b and not b <= a) or not parts <= a


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
    authors = many("select a.name from books_authors_link l join authors a on a.id=l.author where l.book=? order by l.id")
    return {
        "id": book_id,
        "title": title,
        "authors": authors,
        "isbns": sorted({i for i in (isbn13(identifiers.get("isbn")),) if i}),
        "goodreads": goodreads_id(identifiers.get("goodreads")),
        "fields": {
            "title": title,
            "authors": authors,
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
        if name == name.lower():
            name = re.sub(r"(^|[\s/&-])([a-z])", lambda m: m.group(1) + m.group(2).upper(), name)
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
    often stores a year alone as January 1. Years before 1000 are Hardcover typos ("0012" for 2012)."""
    new = (release_date or "")[:10] or None
    if new and (new[:4] < "1000" or (old and old[:4] == new[:4])):
        return None
    return new


def search_queries(book):
    main = re.split(r"[:(\[]", book["title"])[0].strip()
    qs = [book["title"], main] + ([f"{main} {book['authors'][0]}"] if book["authors"] else [])
    return [q for q in dict.fromkeys(qs) if q]


class Vocabulary:
    """The controlled tag list with its synonyms (tags.json), and author-name overrides (author-overrides.json)."""

    def __init__(self, doc, authors):
        self.tags = {t.casefold(): t for t in doc["tags"]}
        self.synonyms = {}
        for name, targets in (doc.get("synonyms") or {}).items():
            targets = [targets] if isinstance(targets, str) else list(targets)
            unlisted = [t for t in targets if t.casefold() not in self.tags]
            if unlisted:
                raise ValueError(f"synonym {name!r} names unlisted tags {unlisted}")
            self.synonyms[name.casefold()] = [self.tags[t.casefold()] for t in targets]
        self.authors = authors

    def map(self, names):
        """Each name as the list writes it, through its synonyms; names neither listed nor synonyms go."""
        out = []
        for name in names:
            key = name.casefold()
            for t in [self.tags[key]] if key in self.tags else self.synonyms.get(key, []):
                if t not in out:
                    out.append(t)
        return sorted(out[:MAX_TAGS])

    def rewrite(self, name):
        """The listed tags a magic shelf rule's tag becomes; empty if the list has none."""
        if name.casefold() == REVIEW_TAG:
            return [REVIEW_TAG]
        return self.map([name])


def load_vocabulary(tags_path, authors_path):
    with open(tags_path) as t, open(authors_path) as a:
        return Vocabulary(json.load(t), json.load(a))


def title_case(title):
    words = title.split(" ")
    return " ".join(w if i and w in SMALL_WORDS and not words[i - 1].endswith(":") else w[:1].upper() + w[1:]
                    for i, w in enumerate(words))


def pick_title(old, detail):
    """Hardcover's title, only when the book's main title (before any colon or bracket) differs from
    Hardcover's. Hardcover's subtitle is never written: it is often an edition, translation or blurb."""
    title = " ".join((detail.get("title") or "").split())
    if not title or main_title(title) == main_title(old):
        return None
    inverted = re.search(r",\s*(the|a|an)\b", old or "", re.I)  # "Complete Intrepid Saga, The"
    if not inverted and set(norm(title).split()) <= set(norm(old).split()):
        return None
    if " " in title and title[1:] == title[1:].lower():  # Hardcover keeps some titles in sentence case
        title = title_case(title)
    return title


def pick_authors(old, detail, overrides):
    """Hardcover's authors, only when they fix ours: ours share none of them, or Hardcover names no more.
    A co-author is never added: Hardcover's contributors often include narrators."""
    new = list(dict.fromkeys(overrides.get(a, a) for a in contributors(detail)))
    if not new or any("," in a for a in new) or [norm(a) for a in new] == [norm(a) for a in old]:
        return None
    return None if authors_match(old, new) and len(new) > len(old) else new


def series_dispute(old, book_series):
    """A flag when the book's series and position are none of Hardcover's: the library's own stands."""
    entries = [s for s in book_series or [] if (s.get("series") or {}).get("name")]
    if not old or not entries:
        return None
    if any(norm(s["series"]["name"]) == norm(old[0]) and (s.get("position") is None or float(s["position"]) == old[1])
           for s in entries):
        return None
    s = next((s for s in entries if s.get("featured")), entries[0])
    pos = f"{float(s['position']):g}" if s.get("position") is not None else "?"
    return f"series: ours {old[0]} #{old[1]:g}, Hardcover {s['series']['name']} #{pos}"


def pick_tags(old, hardcover, vocab):
    """Hardcover's genres, or failing those the book's own tags, as the controlled list names them.
    The review tag stays."""
    new = vocab.map(hardcover) or vocab.map(old)
    if REVIEW_TAG in old:
        new = sorted(new + [REVIEW_TAG])
    return None if new == sorted(old) else new


def new_fields(book, detail, edition, vocab):
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
        "title": pick_title(old["title"], detail),
        "authors": pick_authors(old["authors"], detail, vocab.authors),
        "identifiers": ids,
        "series": pick_series(old["series"], detail.get("book_series")),
        "description": description_html(detail.get("description")),
        "publisher": publisher,
        "pubdate": pick_pubdate(old["pubdate"], detail.get("release_date")),
        "rating": rating,
        "tags": pick_tags(old["tags"], genres(detail), vocab),
        "cover": cover,
    }


def changes(book, new):
    return {f: {"old": book["fields"][f], "new": new[f]} for f in FIELDS
            if new.get(f) is not None and new[f] != book["fields"][f]}


def search_match(book, hc, threshold, pool):
    """The best candidate of every search so far, searching each of the book's queries until one passes.
    pool caches each query's results, so a second try (title and author swapped) rescores them first."""
    cand, s, ok = choose(book, [c for hits in pool.values() for c in hits], threshold)
    for q in search_queries(book):
        if ok:
            break
        if q not in pool:
            pool[q] = hc.search(q)
            cand, s, ok = choose(book, [c for hits in pool.values() for c in hits], threshold)
    return cand, s, ok


def swapped(book):
    if len(book["authors"]) != 1:
        return None
    return dict(book, title=book["authors"][0], authors=[book["title"]])


def match(books, hc, threshold):
    by_isbn = hc.editions_by_isbn({i for b in books for i in b["isbns"]})
    log(f"ISBN: {sum(1 for b in books if any(i in by_isbn for i in b['isbns']))} books")
    by_gr = hc.books_by_goodreads({b["goodreads"] for b in books if b["goodreads"]})
    log(f"Goodreads: {sum(1 for b in books if b['goodreads'] in by_gr)} books")

    picks = {}
    for n, b in enumerate(books, 1):
        isbn_books = {by_isbn[i]["book_id"] for i in b["isbns"] if i in by_isbn}
        gr_book = by_gr.get(b["goodreads"])
        own = b["fields"]["identifiers"].get("hardcover-id") or ""
        edition = next((by_isbn[i] for i in b["isbns"] if i in by_isbn), None)
        if len(isbn_books) > 1:
            picks[b["id"]] = {"status": "review", "reason": "ISBNs name different Hardcover books"}
        elif isbn_books:
            hid = next(iter(isbn_books))
            note = " (Goodreads ID names another book)" if gr_book and gr_book != hid else ""
            picks[b["id"]] = {"via": "isbn", "hid": hid, "edition": edition, "note": note}
        elif gr_book:
            picks[b["id"]] = {"via": "goodreads", "hid": gr_book}
        elif own.isdigit():
            picks[b["id"]] = {"via": "hardcover-id", "hid": int(own)}
        else:
            pool = {}
            cand, s, ok = search_match(b, hc, threshold, pool)
            swap = None if ok else swapped(b)
            if swap:
                cand2, s2, ok2 = search_match(swap, hc, threshold, pool)
                if ok2:
                    picks[b["id"]] = {"via": "search", "hid": cand2["id"], "score": s2, "as": swap,
                                      "flag": "title and author were swapped"}
                    continue
            if ok:
                picks[b["id"]] = {"via": "search", "hid": cand["id"], "score": s}
            else:
                picks[b["id"]] = {"status": "none", "score": s, "reason": "no search result passed the threshold",
                                  "near": cand}
        if n % 25 == 0:
            log(f"matched {n}/{len(books)} ({hc.calls} requests)")
    return picks


def judge(b, p, d, threshold):
    """A book's plan record before any changes: its match and why."""
    rec = {"book": b["id"], "title": b["title"], "authors": b["authors"], "status": p.get("status"),
           "via": p.get("via"), "score": p.get("score"), "reason": p.get("reason"), "flags": [],
           "genres": genres(d) if d else []}
    near = p.get("near")
    if near:
        rec["hardcover"] = {"id": near["id"], "title": near["title"], "authors": near["authors"]}
    if "hid" in p and not d:
        rec.update(status="review", reason="Hardcover book not found")
    elif d:
        ours = p.get("as", b)
        authors = contributors(d)
        rec["hardcover"] = {"id": d["id"], "title": d["title"], "authors": authors,
                            "url": f"https://hardcover.app/books/{d.get('slug') or d['id']}"}
        theirs = [d["title"]] + ([f"{d['title']}: {d['subtitle']}"] if d.get("subtitle") else [])
        sim = round(title_similarity(ours["title"], theirs), 3)
        if rec["score"] is None:
            rec["score"] = sim
        if not authors_match(ours["authors"], authors):
            rec.update(status="review", reason="no shared author")
        elif p["via"] != "search" and sim < threshold:
            rec.update(status="review", reason=f"title differs ({sim})")
        elif numbers_conflict(ours["title"], d["title"]):
            rec.update(status="review", reason="title differs (part or volume)")
        else:
            rec.update(status="match", reason=(p.get("note") or "").strip() or None)
            if p.get("flag"):
                rec["flags"].append(p["flag"])
    return rec


def share(recs):
    """Two books matching one Hardcover book are duplicates or a wrong match: both go to review."""
    by_hid = defaultdict(list)
    for r in recs:
        if r["status"] == "match":
            by_hid[r["hardcover"]["id"]].append(r["book"])
    for r in recs:
        hid = (r.get("hardcover") or {}).get("id")
        others = [str(i) for i in by_hid.get(hid, []) if i != r["book"]] if r["status"] == "match" else []
        if others:
            r.update(status="review", reason=f"Hardcover book {hid} also matched by {', '.join(others)}")


def positions(details):
    """The series positions an omnibus's details name ("1-3", "1-3, 5"), or None if they name none clearly."""
    out = set()
    for part in re.split(r",|&|\band\b", details or ""):
        m = re.fullmatch(r"\s*(\d+)\s*(?:[-–]\s*(\d+)\s*)?", part)
        if not m:
            return None
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if hi < lo or hi - lo > MAX_OMNIBUS:
            return None
        out.update(range(lo, hi + 1))
    return out or None


def collected(detail):
    """Each series entry of an omnibus (a compilation naming 2 or more whole positions), with its positions.
    Hardcover marks some single novels and novella anthologies as compilations too."""
    if not detail.get("compilation"):
        return []
    out = []
    for s in detail.get("book_series") or []:
        want = positions(s.get("details")) if (s.get("series") or {}).get("id") else None
        if want and len(want) > 1:
            out.append((s, want))
    return out


def omnibuses(recs, details):
    """A matched omnibus goes when other matched books hold every book it collects, and is flagged when
    they hold only some. One whose books are held nowhere else is how the library has them."""
    matched = [(r, details[r["hardcover"]["id"]]) for r in recs if r["status"] == "match"]
    held = defaultdict(set)
    for r, d in matched:
        for s in [] if collected(d) else d.get("book_series") or []:
            pos = s.get("position")
            if (s.get("series") or {}).get("id") and pos is not None and float(pos).is_integer():
                held[s["series"]["id"]].add(int(pos))
    for r, d in matched:
        entries = [(s, want, want - held[s["series"]["id"]]) for s, want in collected(d)]
        if not entries:
            continue
        if not any(missing for _, _, missing in entries):
            r["delete"] = "omnibus: holds " + "; ".join(f"{s['series']['name']} {s['details']}" for s, _, _ in entries)
        elif any(want - missing for _, want, missing in entries):
            r["flags"] += [f"omnibus: {s['series']['name']} {', '.join(map(str, sorted(missing)))} not held"
                           for s, _, missing in entries if missing]


def plan(books, hc, vocab, threshold=THRESHOLD):
    picks = match(books, hc, threshold)
    details = hc.details({p["hid"] for p in picks.values() if "hid" in p})
    recs = [judge(b, picks[b["id"]], details.get(picks[b["id"]].get("hid")), threshold) for b in books]
    share(recs)
    omnibuses(recs, details)
    for b, r in zip(books, recs):
        if r.get("delete"):
            r["changes"] = {}
            yield r
            continue
        old = b["fields"]
        if r["status"] == "match":
            p = picks[b["id"]]
            new = new_fields(b, details[p["hid"]], p.get("edition"), vocab)
            dispute = series_dispute(old["series"], details[p["hid"]].get("book_series"))
            if dispute:
                r["flags"].append(dispute)
        else:
            new = {"tags": pick_tags(old["tags"], [], vocab)}
            r["flags"].insert(0, f"{'unmatched' if r['status'] == 'none' else 'review'}: {r['reason']}")
        tags = new["tags"] if new.get("tags") is not None else sorted(old["tags"])
        if r["flags"] and REVIEW_TAG not in tags:
            tags = sorted(tags + [REVIEW_TAG])
        r["changes"] = changes(b, dict(new, tags=tags))
        yield r


# --- magic shelves ---------------------------------------------------------------------------

def tag_rules(rules):
    """Each leaf rule testing a tag, with the list holding it and its index there."""
    for i, r in enumerate(rules.get("rules") or []):
        if "rules" in r:
            yield from tag_rules(r)
        elif (r.get("field") or r.get("id")) == "tag" and isinstance(r.get("value"), str):
            yield rules["rules"], i, r


def rewrite_rules(rules, mapping):
    """Each tag rule's value through mapping; a tag that becomes several becomes a group of rules, any
    of them for a positive operator and all of them for a negated one."""
    for siblings, i, r in list(tag_rules(rules)):
        new = mapping.get(r["value"])
        if not new:
            continue
        if len(new) == 1:
            r["value"] = new[0]
        else:
            group = "AND" if r.get("operator", "").startswith("not_") else "OR"
            siblings[i] = {"condition": group, "rules": [{**r, "value": t} for t in new]}


def shelf_plan(app_db, vocab):
    """Every magic shelf rule naming a tag the list lacks, with what it becomes (empty if nothing, so the
    rule stays and matches no Book), and a private Review shelf for each admin without one."""
    db = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True)
    for sid, name, rules in db.execute("select id, name, rules from magic_shelf order by id"):
        tags = dict.fromkeys(r["value"] for _, _, r in tag_rules(json.loads(rules or "{}")))
        rewrites = [[t, vocab.rewrite(t)] for t in tags if vocab.rewrite(t) != [t]]
        if rewrites:
            yield {"kind": "shelf", "shelf": sid, "name": name, "rewrites": rewrites}
    for (uid,) in db.execute("select id from user where role & 1 order by id"):
        if not has_review_shelf(db, uid):
            yield {"kind": "shelf", "create": REVIEW_SHELF, "user": uid}


def has_review_shelf(db, uid):
    return db.execute("select 1 from magic_shelf where user_id=? and name=? and not is_system",
                      (uid, REVIEW_SHELF)).fetchone() is not None


def apply_shelves(records, app_db):
    db = sqlite3.connect(app_db)
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    for r in records:
        if "create" in r and has_review_shelf(db, r["user"]):
            out = {"shelf": r["create"], "user": r["user"], "result": "skipped"}
        elif "create" in r:
            db.execute("insert into magic_shelf (uuid, name, is_public, is_system, user_id, icon, rules, kobo_sync, "
                       "created, last_modified) values (?, ?, 0, 0, ?, ?, ?, 0, ?, ?)",
                       (str(uuid.uuid4()), r["create"], r["user"], "🔍", json.dumps(REVIEW_RULES), now, now))
            out = {"shelf": r["create"], "user": r["user"], "result": "created"}
        else:
            mapping = {old: new for old, new in r["rewrites"] if new}
            row = db.execute("select rules from magic_shelf where id=?", (r["shelf"],)).fetchone()
            if not row or not mapping:
                print(json.dumps({"shelf": r["shelf"], "result": "skipped"}), flush=True)
                continue
            rules = json.loads(row[0])
            rewrite_rules(rules, mapping)
            db.execute("update magic_shelf set rules=?, last_modified=? where id=?", (json.dumps(rules), now, r["shelf"]))
            if db.execute("select 1 from sqlite_master where name='magic_shelf_cache'").fetchone():
                db.execute("delete from magic_shelf_cache where shelf_id=?", (r["shelf"],))
            out = {"shelf": r["shelf"], "result": "rewritten", "rewrites": sorted(mapping.items()),
                   "unmapped": [old for old, new in r["rewrites"] if not new]}
        print(json.dumps(out, ensure_ascii=False), flush=True)
    db.commit()


# --- applying --------------------------------------------------------------------------------

def calibredb_fields(chg, cover_path=None):
    args = []
    for f, c in chg.items():
        v = c["new"]
        if f == "title":
            args.append(f"title:{v}")
        elif f == "authors":
            args.append("authors:" + " & ".join(v))
        elif f == "identifiers":
            args.append("identifiers:" + ",".join(f"{k}:{val}" for k, val in sorted(v.items())))
        elif f == "series":
            args += [f"series:{v[0]}", f"series_index:{v[1]:g}"]
        elif f == "description":
            args.append(f"comments:{v}")
        elif f == "publisher":
            args.append(f"publisher:{v}")
        elif f == "pubdate":
            # A bare date goes through calibre's parse_only_date, which may move the day by one
            args.append(f"pubdate:{v}T00:00:00+00:00")
        elif f == "rating":
            args.append(f"rating:{v / 2:g}")
        elif f == "tags":
            args.append("tags:" + ",".join(v))
        elif f == "cover" and cover_path:
            args.append(f"cover:{cover_path}")
    return [x for a in args for x in ("--field", a)]


def folded(value):
    """calibre's tags and publishers are case-insensitive and shared: writing "adult" on one book renames
    "Adult" on every book, so drift ignores case."""
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return sorted(v.casefold() for v in value)
    return value


def drifted(rec, now):
    return [f for f, c in rec["changes"].items() if folded(now["fields"][f]) != folded(c["old"])]


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "cwa-hardcover"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        out.write(resp.read())
    return image_area(dest) is not None


def run_calibredb(args):
    r = subprocess.run(args, capture_output=True, text=True, env=dict(os.environ, HOME=tempfile.gettempdir()))
    return r.returncode, (r.stderr or r.stdout).strip()


def apply(records, library, only=None, run=run_calibredb):
    """Write each book's changes then embed them in its files, and remove each omnibus marked to go."""
    db = connect(library)
    counts = Counter()
    cli = ["calibredb", "--with-library", library]
    with tempfile.TemporaryDirectory() as tmp:
        for rec in records:
            if (only and rec["book"] not in only) or not (rec.get("changes") or rec.get("delete")):
                continue
            now = current(db, library, rec["book"])
            out = {"book": rec["book"], "title": rec["title"]}
            moved = drifted(rec, now) if now else ["book"]
            if moved:
                out.update(result="skipped", reason="changed since plan: " + ",".join(moved))
            elif rec.get("delete"):
                rc, msg = run([*cli, "remove", "--permanent", str(rec["book"])])
                out.update(result="failed", reason=msg.splitlines()[-1:]) if rc else out.update(result="removed")
            else:
                cover = None
                if "cover" in rec["changes"]:
                    cover = os.path.join(tmp, f"{rec['book']}.img")
                    try:
                        if not download(rec["changes"]["cover"]["new"]["url"], cover):
                            cover = None
                    except Exception:
                        cover = None
                rc, msg = run([*cli, "set_metadata", str(rec["book"]), *calibredb_fields(rec["changes"], cover)])
                if not rc:
                    rc, msg = run([*cli, "embed_metadata", str(rec["book"])])
                if rc:
                    out.update(result="failed", reason=msg.splitlines()[-1:])
                else:
                    out.update(result="applied", fields=sorted(f for f in rec["changes"] if f != "cover" or cover))
            counts[out["result"]] += 1
            print(json.dumps(out, ensure_ascii=False), flush=True)
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
    w.writerow(["book", "status", "via", "score", "reason", "flags", "delete", "title", "authors", "hardcover_title",
                "hardcover_authors", "url", *[f"{f}_{s}" for f in FIELDS for s in ("old", "new")]])
    status, removed, flags = Counter(), Counter(), Counter()
    for r in records:
        if r.get("kind") == "shelf":
            log(f"shelf   {json.dumps({k: v for k, v in r.items() if k != 'kind'}, ensure_ascii=False)}")
            gone = [old for old, new in r.get("rewrites") or [] if not new]
            if gone:
                log(f"WARNING shelf {r['shelf']} {r['name']!r}: rules on {', '.join(gone)} will match no Book")
            continue
        status[(r["status"], r.get("via"))] += 1
        flags.update(f.split(":")[0] for f in r.get("flags") or [])
        flags.update(["delete"] if r.get("delete") else [])
        hc, chg = r.get("hardcover") or {}, r.get("changes") or {}
        if r["status"] == "match" and "tags" in chg:
            removed.update(set(chg["tags"]["old"]) - set(chg["tags"]["new"]))
        w.writerow([r["book"], r["status"], r.get("via") or "", r.get("score") or "", r.get("reason") or "",
                    "; ".join(r.get("flags") or []), r.get("delete") or "", r["title"], ", ".join(r["authors"]), hc.get("title", ""), ", ".join(hc.get("authors", [])),
                    hc.get("url", ""), *[show(chg[f][s]) if f in chg else "" for f in FIELDS for s in ("old", "new")]])
    for (s, via), n in sorted(status.items(), key=lambda x: (x[0][0], x[0][1] or "")):
        log(f"{s:7} {via or '':10} {n}")
    if removed:
        log("tags removed most: " + ", ".join(f"{t} ({n})" for t, n in removed.most_common(10)))
    if flags:
        log("flags: " + ", ".join(f"{f} ({n})" for f, n in flags.most_common()))


def tag_report(records, vocab, out):
    """Each Hardcover genre in a plan, how many books carry it, and the listed tags it maps to."""
    counts = Counter(g for r in records if r.get("kind") != "shelf" for g in r.get("genres") or [])
    w = csv.writer(out)
    w.writerow(["genre", "books", "maps_to"])
    for g, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        w.writerow([g, n, " & ".join(vocab.map([g]))])


# --- CLI -------------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="match books; print one JSON line per book and per magic shelf to change")
    p.add_argument("--library", default=LIBRARY)
    p.add_argument("--app-db", default=APP_DB)
    p.add_argument("--sample", type=int, help="this many random books")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--ids", help="comma-separated book ids")
    p.add_argument("--threshold", type=float, default=THRESHOLD)
    a = sub.add_parser("apply", help="write a reviewed plan (stdin) with calibredb and to app.db")
    a.add_argument("--library", default=LIBRARY)
    a.add_argument("--app-db", default=APP_DB)
    a.add_argument("--only", help="comma-separated book ids; magic shelves are left alone")
    sub.add_parser("report", help="plan (stdin) to CSV (stdout)")
    t = sub.add_parser("tags", help="plan (stdin) to a CSV of Hardcover genres (stdout)")
    for x in (p, t):
        x.add_argument("--tags", default=os.path.join(HERE, "tags.json"), help="the controlled tag list")
        x.add_argument("--author-overrides", default=os.path.join(HERE, "author-overrides.json"))
    args = ap.parse_args(argv)
    lines = lambda: (json.loads(line) for line in sys.stdin if line.strip())  # noqa: E731

    if args.cmd == "report":
        report(lines(), sys.stdout)
        return 0
    if args.cmd == "tags":
        tag_report(lines(), load_vocabulary(args.tags, args.author_overrides), sys.stdout)
        return 0
    if args.cmd == "apply":
        only = {int(x) for x in args.only.split(",")} if args.only else None
        records = list(lines())
        rc = apply([r for r in records if r.get("kind") != "shelf"], args.library, only)
        if not only:
            apply_shelves([r for r in records if r.get("kind") == "shelf"], args.app_db)
        return rc

    token = os.environ.get("HARDCOVER_TOKEN")
    if not token:
        log("HARDCOVER_TOKEN is not set")
        return 2
    vocab = load_vocabulary(args.tags, args.author_overrides)
    db = connect(args.library)
    ids = [r[0] for r in db.execute("select id from books order by id")]
    if args.ids:
        ids = [int(x) for x in args.ids.split(",")]
    elif args.sample:
        ids = sorted(random.Random(args.seed).sample(ids, min(args.sample, len(ids))))
    books = [b for b in (current(db, args.library, i) for i in ids) if b]
    log(f"planning {len(books)} books")
    for rec in plan(books, Hardcover(token), vocab, args.threshold):
        print(json.dumps(rec, ensure_ascii=False), flush=True)
    for rec in shelf_plan(args.app_db, vocab):
        print(json.dumps(rec, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
