"""Tests for cwa_hardcover.py. Hardcover is stubbed; they need no network, library or calibredb."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cwa_hardcover as ch  # noqa: E402

VOCAB = ch.Vocabulary({"tags": ["Fantasy", "Fiction", "Science Fiction"],
                       "synonyms": {"Sci-Fi": "Science Fiction", "Science Fiction & Fantasy": ["Science Fiction", "Fantasy"]}},
                      {"Yoshiro Nabeda Wrong": "Yoshiro Nabeda", "Michal Jaworski": "Michał Jaworski"})


def book(title="Project Hail Mary", authors=("Andy Weir",), isbns=(), goodreads=None, **fields):
    base = {"title": title, "authors": list(authors), "identifiers": {}, "series": None, "description": None, "publisher": None, "pubdate": None,
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
                      None, VOCAB)
    assert n["identifiers"] == {"goodreads": "1", "asin": "B0", "hardcover-id": "10", "hardcover-slug": "book-10",
                                "hardcover-edition": "77", "isbn": "9780593135228"}
    assert n["series"] is None  # an existing series is never renamed or renumbered
    assert ch.new_fields(book(), detail(book_series=[{"position": 2, "featured": False, "series": {"name": "Hail Mary"}},
                                                     {"position": 1, "featured": True, "series": {"name": "Standalone"}}]),
                         None, VOCAB)["series"] == ["Standalone", 1.0]
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
    n = ch.new_fields(b, d, None, VOCAB)
    assert n["identifiers"]["isbn"] == "9780000000002"
    assert [n[f] for f in ("rating", "description", "series", "pubdate", "tags", "cover")] == [None] * 6
    assert set(ch.changes(b, n)) == {"identifiers", "publisher"}


def test_pick_pubdate():
    assert ch.pick_pubdate("2021-03-09", "2021-01-01") is None
    assert ch.pick_pubdate(None, "0012-01-01") is None
    assert ch.pick_pubdate("2020-02-02", "2021-05-04") == "2021-05-04"


def test_unknown_cover_size_is_not_replaced():
    assert ch.new_fields(book(cover={"area": None}), detail(), None, VOCAB)["cover"] is None


def test_rerun_after_apply_changes_nothing():
    # calibre stores a 2000x3000 cover as 1466x2200 and strips the publisher name
    huge = {"url": "https://img/h.jpg", "width": 2000, "height": 3000}
    d = detail(image=huge, default_ebook_edition={"id": 77, "publisher": {"name": "Ballantine "}})
    assert ch.new_fields(book(cover={"area": 3_000_000}), d, None, VOCAB)["cover"]["area"] == 1466 * 2200
    applied = book(cover={"area": 1466 * 2200}, publisher="Ballantine")
    n = ch.new_fields(applied, d, None, VOCAB)
    assert (n["cover"], n["publisher"]) == (None, "Ballantine")


def test_calibredb_fields():
    chg = {f: {"old": None, "new": v} for f, v in {
        "identifiers": {"isbn": "1", "hardcover-id": "2"}, "series": ["S", 2.5], "rating": 9,
        "pubdate": "2021-05-04", "tags": ["A", "B"], "cover": {"url": "u", "area": 1}}.items()}
    assert ch.calibredb_fields(chg, "/tmp/c.jpg") == [
        "--field", "identifiers:hardcover-id:2,isbn:1", "--field", "series:S", "--field", "series_index:2.5",
        "--field", "rating:4.5", "--field", "pubdate:2021-05-04T00:00:00+00:00", "--field", "tags:A,B",
        "--field", "cover:/tmp/c.jpg"]
    assert not any(a.startswith("cover:") for a in ch.calibredb_fields(chg, None))


def test_drifted():
    rec = {"changes": {"tags": {"old": ["A"], "new": ["B"]}, "rating": {"old": None, "new": 8}}}
    assert ch.drifted(rec, book(tags=["A"])) == []
    assert ch.drifted(rec, book(tags=["A", "C"], rating=4)) == ["tags", "rating"]
    # another book's write renamed shared items' case
    rec = {"changes": {"tags": {"old": ["Adult", "Fantasy"], "new": ["B"]}, "publisher": {"old": "Daw", "new": "X"}}}
    assert ch.drifted(rec, book(tags=["Fantasy", "adult"], publisher="DAW")) == []


def test_lowercase_genres_are_capitalized():
    d = detail(cached_tags={"Genre": [{"tag": "science fiction fantasy", "count": 3}, {"tag": "sci-fi", "count": 2},
                                      {"tag": "LGBTQ", "count": 2}]})
    assert ch.genres(d) == ["LGBTQ", "Sci-Fi", "Science Fiction Fantasy"]


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
    recs = {r["book"]: r for r in ch.plan(books, hc, VOCAB)}
    assert (recs[1]["status"], recs[1]["via"], recs[1]["reason"]) == ("match", "isbn",
                                                                      "(Goodreads ID names another book)")
    assert recs[1]["changes"]["identifiers"]["new"]["hardcover-edition"] == "5"  # the ISBN's own edition
    assert (recs[2]["status"], recs[2]["reason"]) == ("review", "no shared author")
    assert (recs[3]["status"], recs[3]["via"], recs[3]["hardcover"]["id"]) == ("match", "search", 30)
    assert recs[4]["status"] == "none" and recs[4]["changes"] == {"tags": {"old": [], "new": ["review"]}}
    assert "Max" in hc.queries and "Max: A Maximum Ride Novel" in hc.queries


# --- Phase 3 (#753): title, authors, series disputes, controlled tags, omnibuses, review flag -------

def hc_author(name):
    return {"author": {"name": name}, "contribution": None}


@pytest.mark.parametrize("ours,title,subtitle,new", [
    # our main title (before a colon) is Hardcover's: ours stands, subtitle and all
    ("Motivational Interviewing: Helping People Change", "Motivational Interviewing", "Helping People Grow", None),
    ("Herokiller: A Novel", "Herokiller", None, None),
    ("Jurassic Park", "Jurassic Park", "A Novel", None),
    ("Time of Contempt", "The Time of Contempt", None, None),
    ("the last wish", "The Last Wish", None, None),
    # Hardcover's has only words ours has: ours keeps its punctuation
    ("Welcome to the Universe: An Astrophysical Tour", "Welcome to the Universe An Astrophysical Tour", None, None),
    ("Tom Clancy's Line of Sight", "Tom Clancy Line of Sight", None, None),
    ("The Renegade Star #1-3", "Renegade Star", None, None),
    # otherwise Hardcover's title, without its subtitle (often an edition or translation note)
    ("Last Wish.azw", "The Last Wish", "Introducing the Witcher", "The Last Wish"),
    ("Complete Intrepid Saga, The", "The Complete Intrepid Saga", None, "The Complete Intrepid Saga"),
    ("Lies of the Beholder", "Legion: Lies of the Beholder", None, "Legion: Lies of the Beholder"),
    ("Old", "The past through tomorrow", None, "The Past Through Tomorrow"),
    ("Old", "Star Wars: The Prequel Trilogy  ", None, "Star Wars: The Prequel Trilogy"),
])
def test_title_is_hardcovers_only_when_the_main_title_differs(ours, title, subtitle, new):
    assert ch.pick_title(ours, {"title": title, "subtitle": subtitle}) == new


@pytest.mark.parametrize("ours,theirs,new", [
    (["Weir, A."], ["Andy Weir", "Ray Porter"], None),  # never adds a co-author (or a mislabelled narrator)
    (["andy weir"], ["Andy Weir"], None),
    (["Magic Dreams"], ["Ilona Andrews"], ["Ilona Andrews"]),
    (["Hammer Of God"], ["Yoshiro Nabeda Wrong", "Andy Weir"], ["Yoshiro Nabeda", "Andy Weir"]),  # none shared
    (["Vince Flynn", "Kyle Mills"], ["Kyle Mills"], ["Kyle Mills"]),
    (["Genett", "Donna M.", "1955-"], ["Donna M. Genett"], ["Donna M. Genett"]),
    (["Michał Jaworski"], ["Michal Jaworski"], None),  # through the overrides
    (["Dan Simmons"], ["Dan Simmons, Dan Simmons"], None),  # a comma in a name is Hardcover's error
])
def test_authors_are_hardcovers_only_when_they_fix_ours(ours, theirs, new):
    assert ch.pick_authors(ours, detail(cached_contributors=[hc_author(a) for a in theirs]), VOCAB.authors) == new


@pytest.mark.parametrize("ours,theirs,conflict", [
    ("The Book of Lost Tales, Part One", "The Book of Lost Tales, Part II", True),
    ("The Past Through Tomorrow Vol 1", "The Past Through Tomorrow: Book Two", True),
    ("C# 7.1 and .NET Core 2.0 - Third Edition", "C# 7.1 and .NET Core 2.0", False),
    ("Fahrenheit 451", "Fahrenheit 451", False),
    ("Dune", "Dune Messiah", False),
    ("Jack Reacher: One Shot: A Novel", "Jack Reacher Novel One Shot Part 2", True),  # only theirs is a part
    ("Alastair Stone Chronicles Box Set", "Alastair Stone Chronicles Box Set, Vol. 2", True),
    ("Catalyst - A Rogue One Novel", "Catalyst: A Rogue One Novel", False),
])
def test_numbered_titles_must_agree(ours, theirs, conflict):
    assert ch.numbers_conflict(ours, theirs) is conflict


def test_a_series_dispute_is_flagged_and_left_alone():
    hc = [{"position": 2, "featured": True, "series": {"id": 1, "name": "Hail Mary"}}]
    assert ch.new_fields(book(series=["Hail Mary", 1.0]), detail(book_series=hc), None, VOCAB)["series"] is None
    assert ch.series_dispute(["Hail Mary", 1.0], hc) == "series: ours Hail Mary #1, Hardcover Hail Mary #2"
    agree = hc + [{"position": 1, "featured": False, "series": {"id": 2, "name": "Hail-Mary"}}]
    assert ch.series_dispute(["Hail Mary", 1.0], agree) is None
    assert ch.series_dispute(None, hc) is None


def test_tags_fit_the_controlled_list():
    d = detail(cached_tags={"Genre": [{"tag": "sci-fi", "count": 5}, {"tag": "Science Fiction & Fantasy", "count": 3},
                                      {"tag": "Audiobook", "count": 9}]})
    assert ch.new_fields(book(tags=["review", "Old"]), d, None, VOCAB)["tags"] == ["Fantasy", "Science Fiction", "review"]
    # Hardcover gives nothing listed: the Book's own tags are mapped instead
    none = detail(cached_tags={"Genre": [{"tag": "Audiobook", "count": 9}]})
    assert ch.new_fields(book(tags=["Sci-Fi", "Audiobook"]), none, None, VOCAB)["tags"] == ["Science Fiction"]


def test_plan_writes_titles_and_flags_unmatched_books():
    books = [dict(book("Project Hail Mary, The", tags=["Sci-Fi", "Junk"]), id=1),
             dict(book("Unfindable", ["Nobody"], tags=["Fantasy", "Junk"]), id=2)]
    hc = StubHardcover({10: detail(10)}, searches={"Project Hail Mary, The": [cand(10, "Project Hail Mary", ["Andy Weir"])]})
    recs = {r["book"]: r for r in ch.plan(books, hc, vocab=VOCAB)}
    assert recs[1]["changes"]["title"]["new"] == "Project Hail Mary"
    assert recs[1]["flags"] == [] and recs[1]["genres"] == ["Fiction", "Science Fiction"]
    assert recs[2]["status"] == "none" and recs[2]["flags"] == ["unmatched: no search result passed the threshold"]
    assert recs[2]["changes"] == {"tags": {"old": ["Fantasy", "Junk"], "new": ["Fantasy", "review"]}}


def test_existing_hardcover_id_needs_a_close_title_and_must_not_be_shared():
    books = [dict(book("Hyperion", ["Dan Simmons"], identifiers={"hardcover-id": "7"}), id=1),
             dict(book("Dune", ["Frank Herbert"], identifiers={"hardcover-id": "8"}), id=2),
             dict(book("Dune (Retail)", ["Frank Herbert"], identifiers={"hardcover-id": "8"}), id=3),
             dict(book("Artemis", ["Andy Weir"], identifiers={"hardcover-id": "9"}), id=4)]
    hc = StubHardcover({7: detail(7, title="The Fall of Hyperion", cached_contributors=[hc_author("Dan Simmons")]),
                        8: detail(8, title="Dune", cached_contributors=[hc_author("Frank Herbert")]),
                        9: detail(9, title="Artemis")})
    recs = {r["book"]: r for r in ch.plan(books, hc, vocab=VOCAB)}
    assert (recs[1]["status"], recs[1]["via"]) == ("review", "hardcover-id") and recs[1]["reason"].startswith("title differs")
    assert recs[2]["status"] == recs[3]["status"] == "review"
    assert recs[2]["flags"] == ["review: Hardcover book 8 also matched by 3"]
    assert (recs[4]["status"], recs[4]["via"]) == ("match", "hardcover-id")
    assert set(recs[1]["changes"]) == {"tags"}  # a review Book gets only its tags mapped and the flag


def test_a_swapped_title_and_author_is_fixed_and_flagged():
    books = [dict(book("Andy Weir", ["Project Hail Mary"]), id=1)]
    hc = StubHardcover({10: detail(10)}, searches={"Andy Weir": [cand(10, "Project Hail Mary", ["Andy Weir"])]})
    rec = next(ch.plan(books, hc, vocab=VOCAB))
    assert (rec["status"], rec["via"], rec["flags"]) == ("match", "search", ["title and author were swapped"])
    assert (rec["changes"]["title"]["new"], rec["changes"]["authors"]["new"]) == ("Project Hail Mary", ["Andy Weir"])


def test_an_omnibus_is_dropped_only_when_every_collected_book_is_held():
    def series(pos, details=None, sid=5):
        return [{"position": pos, "details": details, "featured": True, "series": {"id": sid, "name": "Night's Dawn"}}]
    books = [dict(book(f"Part {i}", identifiers={"hardcover-id": str(i)}), id=i) for i in (1, 2, 3)]
    books += [dict(book(t, identifiers={"hardcover-id": str(i)}), id=i) for i, t in
              ((4, "Trilogy"), (5, "Big Omnibus"), (6, "Anthology"), (7, "Later Trilogy"), (8, "Novellas"))]
    hc = StubHardcover({**{i: detail(i, title=f"Part {i}", book_series=series(i)) for i in (2, 3)},
                        # Hardcover marks some single novels as compilations: they hold their position
                        1: detail(1, title="Part 1", compilation=True, book_series=series(1, "1")),
                        4: detail(4, title="Trilogy", compilation=True, book_series=series(1, "1-3")),
                        5: detail(5, title="Big Omnibus", compilation=True, book_series=series(1, "1-3, 5")),
                        6: detail(6, title="Anthology", compilation=True),
                        7: detail(7, title="Later Trilogy", compilation=True, book_series=series(7, "7-9")),
                        8: detail(8, title="Novellas", compilation=True, book_series=series(2.5, "2.5"))})
    recs = {r["book"]: r for r in ch.plan(books, hc, vocab=VOCAB)}
    assert recs[4]["delete"] == "omnibus: holds Night's Dawn 1-3"
    assert "delete" not in recs[5] and recs[5]["flags"] == ["omnibus: Night's Dawn 5 not held"]  # a partial duplicate
    for i in (1, 6, 7, 8):  # a novel, an anthology, an omnibus holding nothing else held, novellas
        assert "delete" not in recs[i] and recs[i]["flags"] == []


def test_shelves_rewrite_unlisted_tags_and_add_a_review_shelf_per_admin(tmp_path):
    import json
    import sqlite3
    app = sqlite3.connect(tmp_path / "app.db")
    app.executescript("""
        create table user (id integer primary key, role int);
        create table magic_shelf (id integer primary key, uuid text, name text, is_public int, is_system int,
          user_id int, icon text, rules text, kobo_sync int, created text, last_modified text);
        insert into user values (3, 511), (4, 338), (5, 1);""")
    sff = "Science Fiction & Fantasy"
    rule = {"condition": "OR", "rules": [{"id": "tag", "field": "tag", "operator": "equal", "value": "Sci-Fi"},
                                         {"id": "tag", "operator": "equal", "value": sff},
                                         {"id": "tag", "operator": "not_equal", "value": sff},
                                         {"condition": "AND", "rules": [{"id": "tag", "operator": "equal", "value": "Junk"}]}]}
    app.execute("insert into magic_shelf (id,name,user_id,is_system,rules) values (1,'SF',4,0,?)", (json.dumps(rule),))
    app.execute("insert into magic_shelf (id,name,user_id,is_system,rules) values (2,'Review',5,0,?)",
                (json.dumps(ch.REVIEW_RULES),))
    app.commit()
    recs = list(ch.shelf_plan(str(tmp_path / "app.db"), VOCAB))
    assert recs == [{"kind": "shelf", "shelf": 1, "name": "SF",
                     "rewrites": [["Sci-Fi", ["Science Fiction"]], [sff, ["Fantasy", "Science Fiction"]], ["Junk", []]]},
                    {"kind": "shelf", "create": "Review", "user": 3}]
    ch.apply_shelves(recs, str(tmp_path / "app.db"))
    ch.apply_shelves(recs, str(tmp_path / "app.db"))
    rules = json.loads(app.execute("select rules from magic_shelf where id=1").fetchone()[0])
    assert rules["rules"][0]["value"] == "Science Fiction"
    assert rules["rules"][1] == {"condition": "OR", "rules": [{"id": "tag", "operator": "equal", "value": "Fantasy"},
                                                              {"id": "tag", "operator": "equal", "value": "Science Fiction"}]}
    assert rules["rules"][2]["condition"] == "AND"
    assert rules["rules"][3]["rules"][0]["value"] == "Junk"
    assert app.execute("select user_id,is_public,is_system,rules from magic_shelf where id>2").fetchall() == [
        (3, 0, 0, json.dumps(ch.REVIEW_RULES))]


def test_apply_writes_then_embeds_and_drops_omnibuses(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ch, "current", lambda db, lib, i: book(title="Old", tags=["Junk"]))
    monkeypatch.setattr(ch, "connect", lambda lib: None)
    run = lambda args: calls.append(args[3:]) or (0, "")  # noqa: E731
    recs = [{"book": 1, "title": "Old", "status": "match", "flags": [],
             "changes": {"title": {"old": "Old", "new": "New"}, "tags": {"old": ["Junk"], "new": []}}},
            {"book": 2, "title": "Old", "status": "match", "flags": [], "delete": "omnibus: holds X 1-3", "changes": {}},
            {"book": 3, "title": "Old", "status": "none", "flags": ["unmatched"], "changes": {}}]
    assert ch.apply(recs, "/lib", run=run) == 0
    assert calls == [["set_metadata", "1", "--field", "title:New", "--field", "tags:"], ["embed_metadata", "1"],
                     ["remove", "--permanent", "2"]]
