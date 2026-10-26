"""Tests for cwa_hardcover.py. Hardcover is stubbed; they need no network, library or calibredb."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cwa_hardcover as ch  # noqa: E402


def book(title="Project Hail Mary", authors=("Andy Weir",), isbns=(), goodreads=None, **fields):
    base = {"identifiers": {}, "series": None, "description": None, "publisher": None, "pubdate": None,
            "rating": None, "tags": [], "cover": None}
    return {"id": 1, "title": title, "authors": list(authors), "isbns": list(isbns), "goodreads": goodreads,
            "fields": {**base, **fields}}


def cand(id, title, authors, users=0, isbns=()):
    return {"id": id, "title": title, "authors": list(authors), "users_count": users, "isbns": list(isbns)}


def detail(id=10, **kw):
    d = {"id": id, "slug": f"book-{id}", "title": "Project Hail Mary", "description": "One.\n\nTwo & three.",
         "rating": 4.26, "ratings_count": 100, "release_date": "2021-05-04",
         "cached_contributors": [{"author": {"name": "Andy Weir"}, "contribution": None},
                                 {"author": {"name": "Ray Porter"}, "contribution": "Narrator"}],
         "cached_tags": {"Genre": [{"tag": "Science Fiction", "count": 9}, {"tag": "Adulte", "count": 1},
                                   {"tag": "Space, Opera", "count": 4}, {"tag": "Fiction", "count": 3}]},
         "book_series": [], "image": {"url": "https://img/b.jpg", "width": 500, "height": 750},
         "default_ebook_edition": {"id": 77, "isbn_13": "9780593135228", "publisher": {"name": "Ballantine"},
                                   "image": {"url": "https://img/e.jpg", "width": 1000, "height": 1500}},
         "default_physical_edition": None}
    d.update(kw)
    return d


def test_isbn_forms():
    assert ch.isbn13("0-8044-2957-X") == "9780804429573"
    assert ch.isbn13("978-0-316-44161-2") == "9780316441612"
    assert ch.isbn13("B00X") is None
    assert ch.isbn10("9780804429573") == "080442957X"
    assert ch.isbn10("9790000000001") is None


@pytest.mark.parametrize("ours,theirs,ok", [
    (["Weir, Andy"], ["Andy Weir"], True),
    (["J.R.R. Tolkien"], ["J. R. R. Tolkien"], True),
    (["Victoria Walker"], ["Victoria   Walker"], True),
    (["Andy Weir"], ["Good Reads Publishing"], False),
    (["Andy Weir"], ["Bob Weir"], False),
])
def test_authors_match(ours, theirs, ok):
    assert ch.authors_match(ours, theirs) is ok


def test_choose_skips_summaries_and_other_authors():
    hits = [cand(1, "Summary and Analysis of Project Hail Mary by Andy Weir", ["Andy Weir"], 50),
            cand(2, "Project Hail Mary", ["Someone Else"], 900),
            cand(3, "Project Hail Mary", ["Andy Weir"], 400)]
    picked, s, ok = ch.choose(book(), hits)
    assert (picked["id"], s, ok) == (3, 1.0, True)


def test_choose_prefers_more_users_and_trusts_shared_isbn():
    hits = [cand(1, "Project Hail Mary", ["Andy Weir"], 3), cand(2, "Project Hail Mary", ["Andy Weir"], 400)]
    assert ch.choose(book(), hits)[0]["id"] == 2
    odd = cand(3, "PHM (Special Edition)", ["Andy Weir"], isbns=["9780593135204"])
    assert ch.choose(book(isbns=["9780593135204"]), [odd])[1:] == (1.0, True)


def test_choose_rejects_weak_title():
    assert ch.choose(book(), [cand(1, "The Martian", ["Andy Weir"])])[2] is False


def test_search_queries_fall_back_to_main_title_then_author():
    assert ch.search_queries(book("Max: A Maximum Ride Novel", ["James Patterson"])) == [
        "Max: A Maximum Ride Novel", "Max", "Max James Patterson"]


def test_new_fields():
    b = book(identifiers={"goodreads": "1", "asin": "B0"}, series=["Hail Mary", 1.0], pubdate="2020-02-02",
             publisher="Old Pub", rating=6, tags=["Old"], cover={"area": 750_000})
    n = ch.new_fields(b, detail(book_series=[{"position": 1, "featured": True, "series": {"name": "Standalone"}},
                                             {"position": 2, "featured": False, "series": {"name": "Hail Mary"}}]),
                      None)
    assert n["identifiers"] == {"goodreads": "1", "asin": "B0", "hardcover-id": "10", "hardcover-slug": "book-10",
                                "hardcover-edition": "77", "isbn": "9780593135228"}
    assert n["series"] == ["Hail Mary", 2.0]  # the book's own series name, Hardcover's position
    assert n["description"] == "<p>One.</p><p>Two &amp; three.</p>"
    assert n["publisher"] == "Ballantine"
    assert n["pubdate"] == "2021-05-04"
    assert n["rating"] == 9
    assert n["tags"] == ["Fiction", "Science Fiction"]  # 2+ votes, no commas
    assert n["cover"] == {"url": "https://img/e.jpg", "area": 1_500_000}


def test_new_fields_keeps_what_hardcover_lacks_or_cannot_improve():
    b = book(isbns=["9780000000002"], identifiers={"isbn": "9780000000002"}, pubdate="2021-03-09",
             cover={"area": 2_000_000})
    d = detail(rating=4.9, ratings_count=2, description=None, book_series=[], release_date="2021-01-01",
               cached_tags={"Genre": [{"tag": "Fiction", "count": 1}]})
    n = ch.new_fields(b, d, None)
    assert n["identifiers"]["isbn"] == "9780000000002"
    assert [n[f] for f in ("rating", "description", "series", "pubdate", "tags", "cover")] == [None] * 6
    assert set(ch.changes(b, n)) == {"identifiers", "publisher"}


def test_unknown_cover_size_is_not_replaced():
    assert ch.new_fields(book(cover={"area": None}), detail(), None)["cover"] is None


def test_rerun_after_apply_changes_nothing():
    # calibre stores a 2000x3000 cover as 1466x2200 and strips the publisher name
    huge = {"url": "https://img/h.jpg", "width": 2000, "height": 3000}
    d = detail(image=huge, default_ebook_edition={"id": 77, "publisher": {"name": "Ballantine "}})
    assert ch.new_fields(book(cover={"area": 3_000_000}), d, None)["cover"]["area"] == 1466 * 2200
    applied = book(cover={"area": 1466 * 2200}, publisher="Ballantine")
    n = ch.new_fields(applied, d, None)
    assert (n["cover"], n["publisher"]) == (None, "Ballantine")


def test_calibredb_fields():
    chg = {f: {"old": None, "new": v} for f, v in {
        "identifiers": {"isbn": "1", "hardcover-id": "2"}, "series": ["S", 2.5], "rating": 9,
        "pubdate": "2021-05-04", "tags": ["A", "B"], "cover": {"url": "u", "area": 1}}.items()}
    assert ch.calibredb_fields(chg, "/tmp/c.jpg") == [
        "--field", "identifiers:hardcover-id:2,isbn:1", "--field", "series:S", "--field", "series_index:2.5",
        "--field", "rating:4.5", "--field", "pubdate:2021-05-04", "--field", "tags:A,B",
        "--field", "cover:/tmp/c.jpg"]
    assert not any(a.startswith("cover:") for a in ch.calibredb_fields(chg, None))


def test_drifted():
    rec = {"changes": {"tags": {"old": ["A"], "new": ["B"]}, "rating": {"old": None, "new": 8}}}
    assert ch.drifted(rec, book(tags=["A"])) == []
    assert ch.drifted(rec, book(tags=["A", "C"], rating=4)) == ["tags", "rating"]


class StubHardcover:
    def __init__(self, details, isbn=None, goodreads=None, searches=None):
        self._details, self.isbn, self.gr, self.searches, self.queries = details, isbn or {}, goodreads or {}, \
            searches or {}, []
        self.calls = 0

    def editions_by_isbn(self, i13s):
        return {i: e for i, e in self.isbn.items() if i in i13s}

    def books_by_goodreads(self, ids):
        return {g: b for g, b in self.gr.items() if g in ids}

    def search(self, q):
        self.queries.append(q)
        return self.searches.get(q, [])

    def details(self, ids):
        return {i: d for i, d in self._details.items() if i in ids}


def test_plan_tiers():
    books = [
        dict(book(isbns=["9780593135204"], goodreads="999"), id=1),
        dict(book("The Martian", goodreads="555"), id=2),
        dict(book("Max: A Maximum Ride Novel", ["James Patterson"]), id=3),
        dict(book("Unfindable", ["Nobody"]), id=4),
    ]
    hc = StubHardcover(
        details={10: detail(10), 20: detail(20, title="The Martian", cached_contributors=[
                     {"author": {"name": "Yoshiro Nabeda"}, "contribution": None}]),
                 30: detail(30, title="Max", cached_contributors=[
                     {"author": {"name": "James Patterson"}, "contribution": None}])},
        isbn={"9780593135204": {"book_id": 10, "id": 5}},
        goodreads={"999": 11, "555": 20},
        searches={"Max James Patterson": [cand(30, "Max", ["James Patterson"], 10)]})
    recs = {r["book"]: r for r in ch.plan(books, hc)}
    assert (recs[1]["status"], recs[1]["via"], recs[1]["reason"]) == ("match", "isbn",
                                                                      "(Goodreads ID names another book)")
    assert recs[1]["changes"]["identifiers"]["new"]["hardcover-edition"] == "5"  # the ISBN's own edition
    assert (recs[2]["status"], recs[2]["reason"]) == ("review", "no shared author")
    assert (recs[3]["status"], recs[3]["via"], recs[3]["hardcover"]["id"]) == ("match", "search", 30)
    assert recs[4]["status"] == "none" and "changes" not in recs[4]
    assert "Max" in hc.queries and "Max: A Maximum Ride Novel" in hc.queries
