"""Tests for cwa_consolidate.py against a stub library: metadata.db, app.db, cwa.db and real EPUB files.
calibredb is replaced by a recorder; they need no CWA, calibre or network."""
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cwa_consolidate as cc  # noqa: E402

METADATA_SCHEMA = """
create table books (id integer primary key, title text, path text, timestamp text, pubdate text, has_cover int default 0);
create table authors (id integer primary key, name text);
create table books_authors_link (id integer primary key, book int, author int);
create table data (id integer primary key, book int, format text, uncompressed_size int, name text);
create table languages (id integer primary key, lang_code text);
create table books_languages_link (id integer primary key, book int, lang_code int, item_order int default 0);
create table tags (id integer primary key, name text);
create table books_tags_link (id integer primary key, book int, tag int);
create table identifiers (id integer primary key, book int, type text, val text);
create table ratings (id integer primary key, rating int);
create table books_ratings_link (id integer primary key, book int, rating int);
"""
APP_SCHEMA = """
create table downloads (id integer primary key, book_id int, user_id int);
create table archived_book (id integer primary key, user_id int, book_id int, is_archived int, last_modified text);
create table book_read_link (id integer primary key, book_id int, user_id int, read_status int,
  last_modified text, last_time_started_reading text, times_started_reading int);
create table book_shelf_link (id integer primary key, book_id int, "order" int, shelf int, date_added text);
create table bookmark (id integer primary key, user_id int, book_id int, format text, bookmark_key text);
create table magic_shelf (id integer primary key, name text, user_id int, is_system int, rules text);
create table magic_shelf_cache (id integer primary key, shelf_id int, user_id int, book_ids text);
create table kobo_reading_state (id integer primary key, user_id int, book_id int, last_modified text);
create table kobo_bookmark (id integer primary key, kobo_reading_state_id int, progress_percent float);
"""
CWA_SCHEMA = """
create table cwa_duplicate_cache (id integer primary key, duplicate_groups_json text);
create table cwa_user_activity (id integer primary key, user_id int, event_type text, item_id int);
"""

CONVERTED_OPF = ('<package><metadata><dc:language>{lang}</dc:language>'
                 '<dc:contributor opf:role="bkp">calibre (5.0) [https://calibre-ebook.com]</dc:contributor>'
                 '</metadata></package>')
RETAIL_OPF = "<package><metadata><dc:language>{lang}</dc:language></metadata></package>"


def epub(path, retail=True, lang="en", toc=3, size_pad=0):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("OEBPS/content.opf", (RETAIL_OPF if retail else CONVERTED_OPF).format(lang=lang))
        z.writestr("OEBPS/toc.ncx", "<ncx><navMap>" + "<navPoint><text>x</text></navPoint>" * toc + "</navMap></ncx>")
        z.writestr("OEBPS/text.xhtml", '<html><body><p class="x">' + "a" * size_pad + "</p></body></html>")
        if not retail:
            z.writestr("OEBPS/page_styles.css", "")


class Lib:
    """A stub library. add() makes a Book and its files; returns its id."""

    def __init__(self, root):
        self.root = root
        self.meta = sqlite3.connect(root / "metadata.db")
        self.meta.executescript(METADATA_SCHEMA)
        self.app = sqlite3.connect(root / "app.db")
        self.app.executescript(APP_SCHEMA)
        self.cwa = sqlite3.connect(root / "cwa.db")
        self.cwa.executescript(CWA_SCHEMA)
        self.cwa.execute("insert into cwa_duplicate_cache (duplicate_groups_json) values ('[]')")
        self.next = 1

    def add(self, title, author="Andy Weir", formats=("EPUB",), lang="eng", tags=(), hardcover=None,
            retail=True, opf_lang="en", toc=3, pad=0, sizes=None):
        i = self.next
        self.next += 1
        path = f"{author}/{title} ({i})"
        (self.root / path).mkdir(parents=True)
        self.meta.execute("insert into books (id,title,path,timestamp) values (?,?,?,?)", (i, title, path, f"2020-01-{i:02d}"))
        aid = self._id("authors", "name", author)
        self.meta.execute("insert into books_authors_link (book,author) values (?,?)", (i, aid))
        if lang:
            self.meta.execute("insert into books_languages_link (book,lang_code) values (?,?)",
                              (i, self._id("languages", "lang_code", lang)))
        for t in tags:
            self.meta.execute("insert into books_tags_link (book,tag) values (?,?)", (i, self._id("tags", "name", t)))
        if hardcover:
            self.meta.execute("insert into identifiers (book,type,val) values (?,?,?)", (i, "hardcover-id", str(hardcover)))
        name = f"{title} - {author}"
        for f in formats:
            p = self.root / path / f"{name}.{f.lower()}"
            if f == "EPUB":
                epub(p, retail=retail, lang=opf_lang, toc=toc, size_pad=pad)
            else:
                p.write_bytes(b"x" * ((sizes or {}).get(f, 100)))
            self.meta.execute("insert into data (book,format,uncompressed_size,name) values (?,?,?,?)",
                              (i, f, p.stat().st_size, name))
        self.meta.commit()
        return i

    def _id(self, table, col, val):
        row = self.meta.execute(f"select id from {table} where {col}=?", (val,)).fetchone()
        if row:
            return row[0]
        return self.meta.execute(f"insert into {table} ({col}) values (?)", (val,)).lastrowid

    def cwa_groups(self, *groups):
        self.cwa.execute("update cwa_duplicate_cache set duplicate_groups_json=?",
                         (json.dumps([{"book_ids": list(g)} for g in groups]),))
        self.cwa.commit()

    def plan(self, epubcheck=None):
        return list(cc.plan(cc.load(str(self.root), str(self.root / "app.db"), str(self.root / "cwa.db")),
                            epubcheck or {}))


@pytest.fixture
def lib(tmp_path):
    return Lib(tmp_path)


def books(records):
    return {r["book"]: r for r in records if r["kind"] == "book"}


def groups(records):
    return [r for r in records if r["kind"] == "group"]


# --- deletions -------------------------------------------------------------------------------

def test_deletes_comics_non_english_and_empty_books(lib):
    cbr = lib.add("Calvin", formats=("CBR",))
    tagged = lib.add("Watchmen", formats=("EPUB",), tags=("Graphic Novels",))
    dutch = lib.add("Het Boek", lang="nld")
    empty = lib.add("Nothing", formats=())
    keep = lib.add("Prose", tags=("Biographical fiction",))
    b = books(lib.plan())
    assert b[cbr]["action"] == "delete" and b[cbr]["reason"] == "comic: CBR"
    assert b[tagged]["action"] != "delete" and b[tagged]["flags"] == ["comic? tagged Graphic Novels"]
    assert b[dutch]["action"] == "delete" and b[dutch]["reason"] == "language: nld"
    assert b[empty]["action"] == "delete" and b[empty]["reason"] == "no files"
    assert b[keep]["action"] == "keep"


def test_unset_language_reads_the_epub(lib):
    en = lib.add("English", lang=None, opf_lang="en-GB")
    de = lib.add("Deutsch", lang=None, opf_lang="de")
    unknown = lib.add("Unknown", lang=None, opf_lang="und")
    mobi = lib.add("Mobi only", lang=None, formats=("MOBI",))
    b = books(lib.plan())
    assert b[en]["action"] == "keep" and not b[en]["flags"]
    assert b[de]["action"] == "delete" and b[de]["reason"] == "language: de (from EPUB)"
    assert b[unknown]["flags"] == ["language unknown"]
    assert b[mobi]["flags"] == ["language unknown"]


# --- one Format per Book ---------------------------------------------------------------------

def test_keeps_epub_and_removes_other_formats(lib):
    i = lib.add("Many", formats=("EPUB", "MOBI", "AZW3", "PDF", "MD", "ORIGINAL_MOBI"))
    r = books(lib.plan())[i]
    assert r["keep"] == "EPUB" and r["convert_from"] is None
    assert sorted(r["remove"]) == ["AZW3", "MD", "MOBI", "ORIGINAL_MOBI", "PDF"]
    assert r["decided_by"] == "1 format"


def test_converts_best_reflowable_format_when_no_epub(lib):
    i = lib.add("Kindle", formats=("MOBI", "AZW3", "PDF"))
    r = books(lib.plan())[i]
    assert r["keep"] == "EPUB" and r["convert_from"] == "AZW3"
    assert sorted(r["remove"]) == ["AZW3", "MOBI", "PDF"]


def test_pdf_only_book_is_kept_and_flagged(lib):
    i = lib.add("Textbook", formats=("PDF", "KFX"))
    r = books(lib.plan())[i]
    assert r["keep"] == "PDF" and r["remove"] == ["KFX"]
    assert r["flags"] == ["PDF only: confirm layout"]


def test_book_without_usable_format_goes_to_review(lib):
    i = lib.add("Locked", formats=("KFX", "ZIP"))
    r = books(lib.plan())[i]
    assert r["action"] == "review" and r["reason"] == "no usable Format: KFX, ZIP"


def test_single_epub_needs_nothing(lib):
    i = lib.add("Clean")
    r = books(lib.plan())[i]
    assert r["action"] == "keep" and r["remove"] == [] and r["convert_from"] is None


# --- duplicates ------------------------------------------------------------------------------

def test_calibre_named_as_producer_alone_does_not_make_an_epub_converted(tmp_path):
    p = tmp_path / "edited.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("OEBPS/content.opf", CONVERTED_OPF.format(lang="en"))
        z.writestr("OEBPS/text.xhtml", "<html><body><p>x</p></body></html>")
    assert cc.inspect_epub(str(p))["retail"] is True


def test_title_and_first_author_group_survivor_holds_retail_epub(lib):
    conv = lib.add("Project Hail Mary", retail=False, pad=5000)
    retail = lib.add("Project Hail Mary [Unabridged]", author="Weir, Andy", formats=("EPUB", "MOBI"))
    other = lib.add("The Martian")
    lib.add("Wheel of Time: The Eye of the World", author="Robert Jordan")
    lib.add("Wheel of Time: The Great Hunt", author="Robert Jordan")
    recs = lib.plan()
    [g] = groups(recs)
    assert g["survivor"] == retail and g["losers"] == [conv]
    assert g["decided_by"] == "2 retail EPUB" and g["status"] == "merge"
    b = books(recs)
    assert b[conv]["action"] == "merge" and b[conv]["into"] == retail
    assert b[retail]["keep"] == "EPUB" and b[retail]["remove"] == ["MOBI"]
    assert b[other]["action"] == "keep"


def test_cwa_cache_and_hardcover_id_also_group(lib):
    a = lib.add("Dune", author="Frank Herbert", hardcover=7)
    b = lib.add("Dune (Dune Chronicles 1)", author="F. Herbert", hardcover=7, toc=1)
    c = lib.add("Odd Title", author="Someone")
    d = lib.add("Odd Title", author="Anonymous", formats=("MOBI",))
    lib.cwa_groups((c, d))
    gs = {tuple(sorted([g["survivor"], *g["losers"]])): g for g in groups(lib.plan())}
    assert gs[(a, b)]["survivor"] == a and gs[(a, b)]["decided_by"] == "4 table of contents"
    assert gs[(c, d)]["survivor"] == c and gs[(c, d)]["decided_by"] == "1 format"


def test_conflicting_hardcover_ids_go_to_review(lib):
    a = lib.add("Collected Stories", hardcover=1)
    b = lib.add("Collected Stories", hardcover=2)
    recs = lib.plan()
    [g] = groups(recs)
    assert g["status"] == "review" and "Hardcover IDs differ" in g["reason"]
    assert books(recs)[a]["action"] == "keep" and books(recs)[b]["action"] == "keep"


def test_deleted_books_never_join_a_group(lib):
    lib.add("Same", lang="nld")
    lib.add("Same")
    assert groups(lib.plan()) == []


def test_different_titles_sharing_a_hardcover_id_go_to_review(lib):
    lib.add("Hyperion", author="Dan Simmons", hardcover=5)
    lib.add("The Fall of Hyperion", author="Dan Simmons", hardcover=5)
    a = lib.add("Dune: The Battle of Corrin", author="Brian Herbert", hardcover=6)
    b = lib.add("The Battle of Corrin", author="Brian Herbert", hardcover=6, formats=("MOBI",))
    gs = sorted(groups(lib.plan()), key=lambda g: g["status"])
    assert [(g["status"], g["reason"]) for g in gs] == [("merge", None), ("review", "titles differ")]
    assert gs[0]["survivor"] == a and gs[0]["losers"] == [b]


def test_epubcheck_then_size_then_existing(lib):
    a = lib.add("Twin")
    b = lib.add("Twin", pad=4000)
    recs = lib.plan()
    [g] = groups(recs)
    assert g["survivor"] == b and g["decided_by"] == "5 larger file"
    assert len(g["epubcheck_missing"]) == 2 and cc.epub_paths(recs) == sorted(g["epubcheck_missing"])
    checks = {p: {"fatal": 0, "errors": 3 if "(2)" in p else 0} for p in g["epubcheck_missing"]}
    [g] = groups(lib.plan(checks))
    assert g["survivor"] == a and g["decided_by"] == "3 epubcheck" and not g["epubcheck_missing"]


def test_identical_files_keep_the_existing_book(lib):
    a = lib.add("Same Size")
    lib.add("Same Size")
    checks = {}
    [g] = groups(lib.plan(checks))
    assert g["survivor"] == a and g["decided_by"] == "6 existing"


# --- apply -----------------------------------------------------------------------------------

class FakeCalibre:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a)) or True


def test_apply_merges_history_identifiers_then_formats_and_flags(lib):
    loser = lib.add("Project Hail Mary", retail=False, hardcover=42)
    survivor = lib.add("Project Hail Mary", formats=("EPUB", "MOBI"))
    pdf = lib.add("Textbook", formats=("PDF",), tags=("Science",))
    lib.app.executescript(f"""
        insert into downloads (book_id,user_id) values ({loser},4),({loser},6),({survivor},6);
        insert into archived_book (user_id,book_id,is_archived) values (4,{loser},1);
        insert into book_shelf_link (book_id,shelf) values ({loser},1);
        insert into magic_shelf_cache (shelf_id,user_id,book_ids) values (1,4,'[1]');""")
    lib.app.commit()
    lib.cwa.executescript(f"insert into cwa_user_activity (user_id,event_type,item_id) values (4,'EMAIL',{loser}),(4,'SEARCH',{loser});")
    lib.cwa.commit()
    recs = lib.plan()
    cal = FakeCalibre()
    assert cc.apply(recs, str(lib.root), str(lib.root / "app.db"), cal, cwa_db=str(lib.root / "cwa.db")) == 0
    app = sqlite3.connect(lib.root / "app.db")
    assert sorted(app.execute("select book_id,user_id from downloads")) == [(survivor, 4), (survivor, 6)]
    assert app.execute("select book_id from archived_book").fetchall() == [(survivor,)]
    assert app.execute("select book_id from book_shelf_link").fetchall() == [(survivor,)]
    assert app.execute("select count(*) from magic_shelf_cache").fetchone() == (0,)
    cwa = sqlite3.connect(lib.root / "cwa.db")
    assert sorted(cwa.execute("select event_type,item_id from cwa_user_activity")) == [("EMAIL", survivor), ("SEARCH", loser)]
    assert cal.calls == [
        ("set_identifiers", survivor, {"hardcover-id": "42"}),
        ("remove_book", loser),
        ("remove_format", survivor, "MOBI"),
        ("embed_metadata", survivor),
        ("add_tag", pdf, "review"),
    ]


def test_merge_keeps_the_furthest_read_status_and_kobo_state(lib):
    loser = lib.add("Read Twice", retail=False)
    survivor = lib.add("Read Twice")
    lib.app.executescript(f"""
        insert into book_read_link (book_id,user_id,read_status,last_modified,times_started_reading)
          values ({loser},4,1,'2024-01-01',1),({survivor},4,0,'2025-01-01',0),({survivor},6,2,'2025-01-01',1);
        insert into kobo_reading_state (id,user_id,book_id,last_modified) values (1,4,{loser},'2025-06-01'),(2,4,{survivor},'2024-01-01');
        insert into kobo_bookmark (kobo_reading_state_id,progress_percent) values (1,80),(2,5);""")
    lib.app.commit()
    cc.apply(lib.plan(), str(lib.root), str(lib.root / "app.db"), FakeCalibre())
    app = sqlite3.connect(lib.root / "app.db")
    assert sorted(app.execute("select user_id,book_id,read_status from book_read_link")) == [(4, survivor, 1), (6, survivor, 2)]
    assert app.execute("select id,book_id from kobo_reading_state").fetchall() == [(1, survivor)]
    assert app.execute("select kobo_reading_state_id,progress_percent from kobo_bookmark").fetchall() == [(1, 80)]


def test_apply_converts_then_removes_sources(lib):
    i = lib.add("Kindle", formats=("MOBI", "AZW3"))
    cal = FakeCalibre()
    cc.apply(lib.plan(), str(lib.root), str(lib.root / "app.db"), cal)
    assert [c[0] for c in cal.calls] == ["convert", "remove_format", "remove_format", "embed_metadata"]
    assert cal.calls[0][1] == i and cal.calls[0][2].endswith(".azw3")
    assert {c[2] for c in cal.calls[1:3]} == {"MOBI", "AZW3"}


def test_a_broken_epub_is_replaced_from_the_next_format(lib):
    i = lib.add("Corrupt", formats=("EPUB", "MOBI"))
    j = lib.add("Corrupt Alone", formats=("EPUB",))
    for book in (i, j):
        next((lib.root).glob(f"*/* ({book})/*.epub")).write_bytes(b"not a zip")
    b = books(lib.plan())
    assert (b[i]["keep"], b[i]["convert_from"], b[i]["remove"]) == ("EPUB", "MOBI", ["MOBI"])
    assert (b[j]["action"], b[j]["reason"]) == ("review", "no usable Format: EPUB (unreadable)")


def test_a_broken_epub_does_not_win_a_group(lib):
    broken = lib.add("Shared")
    mobi = lib.add("Shared", formats=("MOBI",))
    next((lib.root).glob(f"*/* ({broken})/*.epub")).write_bytes(b"not a zip")
    g = groups(lib.plan())[0]
    assert (g["survivor"], g["losers"]) == (mobi, [broken])


def test_apply_skips_a_book_whose_formats_changed(lib):
    i = lib.add("Moving", formats=("EPUB", "MOBI"))
    recs = lib.plan()
    lib.meta.execute("delete from data where book=? and format='MOBI'", (i,))
    lib.meta.commit()
    cal = FakeCalibre()
    cc.apply(recs, str(lib.root), str(lib.root / "app.db"), cal)
    assert cal.calls == []


def test_apply_deletes(lib):
    i = lib.add("Calvin", formats=("CBZ",))
    cal = FakeCalibre()
    cc.apply(lib.plan(), str(lib.root), str(lib.root / "app.db"), cal)
    assert cal.calls == [("remove_book", i)]


def test_apply_skips_books_and_groups_the_operator_kept(lib):
    a = lib.add("Kept Pair")
    lib.add("Kept Pair", pad=4000)
    comic = lib.add("Prose Tagged", tags=("Comics",))
    cal = FakeCalibre()
    cc.apply(lib.plan(), str(lib.root), str(lib.root / "app.db"), cal, skip={a, comic})
    assert cal.calls == []


# --- stats -----------------------------------------------------------------------------------

def test_stats_counts_and_magic_shelves(lib):
    lib.add("One", formats=("EPUB", "MOBI"), hardcover=1, tags=("review",))
    lib.add("Two", formats=("PDF",))
    lib.meta.execute("update books set timestamp='2026-05-01' where id=2")
    lib.meta.commit()
    lib.app.execute("insert into magic_shelf (name,user_id,is_system,rules) values (?,?,?,?)",
                    ("Recently Added", 3, 1, json.dumps({"condition": "AND", "rules": [
                        {"field": "timestamp", "operator": "greater", "value": "2026-02-23"}]})))
    lib.app.commit()
    s = cc.stats(str(lib.root), str(lib.root / "app.db"))
    assert s["books"] == 2 and s["formats"] == {"EPUB": 1, "MOBI": 1, "PDF": 1}
    assert s["multi_format_books"] == 1 and s["hardcover_ids"] == 1 and s["review"] == 1
    assert s["magic_shelves"] == {"3/Recently Added": 1}
