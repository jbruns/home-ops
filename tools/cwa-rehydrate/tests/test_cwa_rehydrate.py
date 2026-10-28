"""Tests for cwa_rehydrate.py against a stub library (metadata.db and real EPUB files), a stub release search
and a stub downloader; they need no CWA, Shelfmark or network."""
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cwa_rehydrate as cr  # noqa: E402

SCHEMA = """
create table books (id integer primary key, title text, path text);
create table authors (id integer primary key, name text);
create table books_authors_link (id integer primary key, book int, author int);
create table data (id integer primary key, book int, format text, uncompressed_size int, name text);
create table identifiers (id integer primary key, book int, type text, val text);
"""
PRODUCER = '<dc:contributor opf:role="bkp">calibre (4.99.5) [https://calibre-ebook.com]</dc:contributor>'


def epub(path, converted=False, producer=False, lang="en", broken=False):
    if broken:
        Path(path).write_bytes(b"not a zip")
        return
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        meta = f"<dc:language>{lang}</dc:language>" + (PRODUCER if producer else "")
        z.writestr("OEBPS/content.opf", f"<package><metadata>{meta}</metadata></package>")
        z.writestr("OEBPS/text.xhtml", '<html><body><p class="calibre1">x</p></body></html>' if converted
                   else "<html><body><p>x</p></body></html>")


class Lib:
    def __init__(self, root):
        self.root = root
        self.db = sqlite3.connect(root / "metadata.db")
        self.db.executescript(SCHEMA)

    def add(self, title, formats=("EPUB",), hardcover=None, isbn=None, **epub_kw):
        i = self.db.execute("insert into books (title, path) values (?, '')", (title,)).lastrowid
        path = f"Andy Weir/{title} ({i})"
        self.db.execute("update books set path = ? where id = ?", (path, i))
        a = self.db.execute("insert into authors (name) values ('Andy Weir')").lastrowid
        self.db.execute("insert into books_authors_link (book, author) values (?, ?)", (i, a))
        (self.root / path).mkdir(parents=True)
        for f in formats:
            p = self.root / path / f"{title}.{f.lower()}"
            epub(p, **epub_kw) if f == "EPUB" else p.write_bytes(b"x")
            self.db.execute("insert into data (book, format, uncompressed_size, name) values (?, ?, 1, ?)",
                            (i, f, title))
        for t, v in (("hardcover-id", hardcover), ("isbn", isbn)):
            if v:
                self.db.execute("insert into identifiers (book, type, val) values (?, ?, ?)", (i, t, str(v)))
        self.db.commit()
        return i


@pytest.fixture
def lib(tmp_path):
    (tmp_path / "lib").mkdir()
    return Lib(tmp_path / "lib")


def release(md5, fmt="epub", lang="en", source="direct_download"):
    return {"source": source, "source_id": md5, "format": fmt, "language": lang,
            "info_url": f"https://aa.example/md5/{md5}", "title": md5}


class Shop:
    """Stub search and downloader: releases per Hardcover ID, and what each md5 downloads as."""

    def __init__(self, releases=None, files=None, left=900):
        self.releases, self.files, self.left = releases or {}, files or {}, left
        self.searched, self.downloaded = [], []

    def search(self, hardcover_id):
        self.searched.append(hardcover_id)
        if self.releases.get(hardcover_id) == "down":
            raise TimeoutError("timed out")
        return self.releases.get(hardcover_id, [])

    def download(self, rel, dest):
        self.downloaded.append(rel["source_id"])
        kind = self.files[rel["source_id"]]
        if kind == "error":
            raise cr.DownloadError("HTTP 502")
        if kind == "quota":
            raise cr.QuotaError("No downloads left")
        if kind == "truncated":
            raise ValueError("https://aa.example/dyn/api/fast_download.json?key=SECRET")
        epub(dest, converted=kind == "converted", lang="fr" if kind == "french" else "en")
        self.left -= 1
        return self.left


@pytest.fixture
def run(tmp_path):
    ingest, staging = tmp_path / "ingest", tmp_path / "staging"
    ingest.mkdir()
    staging.mkdir()
    attempts = tmp_path / "attempts.jsonl"

    def go(cands, shop, **kw):
        status = cr.fetch(cands, search=shop.search, download=shop.download, ingest=ingest, staging=staging,
                          attempts_path=attempts, wait=lambda s: None, **kw)
        rows = [json.loads(x) for x in attempts.read_text().splitlines()] if attempts.exists() else []
        return status, rows

    go.ingest, go.staging = ingest, staging
    return go


def cand(i, kind="converted epub", hc=10, title="Project Hail Mary", formats=("EPUB",)):
    return {"id": i, "title": title, "authors": "Andy Weir", "kind": kind, "hardcover_id": hc,
            "formats": list(formats)}


def test_plan_lists_books_without_a_retail_epub_no_epub_first(lib):
    retail = lib.add("Retail", hardcover=1)
    edited = lib.add("Edited", hardcover=2, producer=True)
    converted = lib.add("Converted", hardcover=3, isbn="9780593135204", converted=True, producer=True)
    broken = lib.add("Broken", hardcover=4, broken=True)
    azw3 = lib.add("Kindle", formats=("AZW3",), hardcover=5)
    pdf = lib.add("Scanned", formats=("PDF",))
    counts, cands = cr.plan(lib.root)
    assert [(c["id"], c["kind"]) for c in cands] == [
        (azw3, "no epub"), (pdf, "no epub"), (broken, "broken epub"), (converted, "converted epub")]
    assert retail not in [c["id"] for c in cands] and edited not in [c["id"] for c in cands]
    c = cands[-1]
    assert (c["hardcover_id"], c["isbn"], c["authors"], c["markers"]) == ("3", "9780593135204", "Andy Weir",
                                                                          ["calibre classes"])
    assert cands[1]["formats"] == ["PDF"] and cands[1]["hardcover_id"] is None
    assert counts == {"books": 6, "retail epub": 2, "converted epub": 1, "broken epub": 1, "no epub": 2,
                      "no hardcover id": 1}


def test_tries_releases_until_a_retail_epub_and_puts_only_that_in_the_ingest_folder(run):
    shop = Shop({10: [release("a"), release("b", fmt="pdf"), release("c"), release("d")]},
                {"a": "converted", "c": "retail", "d": "retail"})
    status, [row] = run([cand(1)], shop)
    assert status == "done" and shop.downloaded == ["a", "c"]
    assert row["result"] == "retail" and row["ingested"] == "c"
    assert [(t["md5"], t["retail"]) for t in row["tries"]] == [("a", False), ("c", True)]
    assert row["available"] == {"epub en": 3, "pdf en": 1}
    [f] = run.ingest.iterdir()
    assert f.name == "Project Hail Mary - c.epub" and cr.inspect_epub(f)["retail"]
    assert list(run.staging.iterdir()) == []


def test_a_converted_book_with_no_retail_copy_is_left_alone_after_three_tries(run):
    shop = Shop({10: [release(m) for m in "abcd"]}, dict.fromkeys("abcd", "converted"))
    _, [row] = run([cand(1)], shop, tries=3)
    assert shop.downloaded == ["a", "b", "c"] and row["result"] == "no retail" and "ingested" not in row
    assert list(run.ingest.iterdir()) == []


def test_a_book_with_no_epub_takes_a_converted_copy_when_no_retail_one_is_found(run):
    shop = Shop({10: [release("a"), release("b")]}, {"a": "error", "b": "converted"})
    _, [row] = run([cand(1, kind="no epub")], shop)
    assert row["result"] == "converted ingested" and row["ingested"] == "b"
    assert row["tries"][0] == {"md5": "a", "error": "HTTP 502"}
    assert [f.name for f in run.ingest.iterdir()] == ["Project Hail Mary - b.epub"]


def test_a_pdf_only_book_takes_only_a_retail_epub_because_its_layout_may_matter(run):
    shop = Shop({10: [release("a")]}, {"a": "converted"})
    _, [row] = run([cand(1, kind="no epub", formats=("PDF",))], shop)
    assert row["result"] == "no retail" and list(run.ingest.iterdir()) == []


def test_an_unexpected_download_error_is_recorded_by_type_only_and_the_next_release_tried(run):
    shop = Shop({10: [release("a"), release("b")]}, {"a": "truncated", "b": "retail"})
    _, [row] = run([cand(1)], shop)
    assert row["tries"][0] == {"md5": "a", "error": "ValueError"} and row["result"] == "retail"
    assert "SECRET" not in json.dumps(row)


def test_a_failed_search_stops_the_run_without_recording_the_book(run):
    shop = Shop({10: [release("a")], 11: "down"}, {"a": "retail"})
    status, rows = run([cand(1), cand(2, hc=11), cand(3)], shop)
    assert status == "search failed" and [r["id"] for r in rows] == [1]


def test_a_non_english_download_is_not_ingested(run):
    shop = Shop({10: [release("a")]}, {"a": "french"})
    _, [row] = run([cand(1, kind="no epub")], shop)
    assert row["result"] == "no retail" and row["tries"][0]["lang"] == "fr"
    assert list(run.ingest.iterdir()) == []


def test_records_which_formats_exist_when_no_epub_release_does(run):
    shop = Shop({10: [release("a", fmt="pdf"), release("b", fmt="mobi"), release("c", fmt="epub", lang="de")]})
    _, [row] = run([cand(1)], shop)
    assert row["result"] == "no epub" and shop.downloaded == []
    assert row["available"] == {"pdf en": 1, "mobi en": 1, "epub de": 1}


def test_a_book_without_a_hardcover_id_is_not_searched(run):
    shop = Shop()
    _, [row] = run([cand(1, hc=None)], shop)
    assert row["result"] == "no hardcover id" and shop.searched == []


def test_stops_before_the_quota_reserve_and_resumes_where_it_stopped(run):
    shop = Shop({10: [release("a")], 11: [release("b")], 12: [release("c")]}, dict.fromkeys("abc", "retail"),
                left=104)
    status, rows = run([cand(1), cand(2, hc=11), cand(3, hc=12)], shop, tries=3, reserve=100)
    # A Book starts only if all its tries fit above the reserve: 104 left, then 103; at 102 Book 3 can't fit
    assert status == "quota" and [r["id"] for r in rows] == [1, 2]
    shop.left = 900
    status, rows = run([cand(1), cand(2, hc=11), cand(3, hc=12)], shop, tries=3, reserve=100)
    assert status == "done" and [r["id"] for r in rows] == [1, 2, 3] and shop.downloaded == ["a", "b", "c"]


def test_stops_when_annas_archive_says_no_downloads_are_left(run):
    shop = Shop({10: [release("a")], 11: [release("b")]}, {"a": "quota", "b": "retail"})
    status, rows = run([cand(1), cand(2, hc=11)], shop)
    assert status == "quota" and rows == [] and shop.downloaded == ["a"]


def test_waits_while_the_ingest_folder_holds_a_backlog(tmp_path):
    ingest, staging = tmp_path / "ingest", tmp_path / "staging"
    ingest.mkdir()
    staging.mkdir()
    for n in range(2):
        (ingest / f"queued{n}.epub").write_bytes(b"x")
    (ingest / "ignored.djvu").write_bytes(b"x")
    waits = []

    def wait(s):
        waits.append(s)
        next(ingest.glob("queued*")).unlink()

    shop = Shop({10: [release("a")]}, {"a": "retail"})
    cr.fetch([cand(1)], search=shop.search, download=shop.download, ingest=ingest, staging=staging,
             attempts_path=tmp_path / "a.jsonl", backlog=2, wait=wait)
    assert len(waits) == 1 and (ingest / "Project Hail Mary - a.epub").exists()


def test_report_lists_every_book_the_after_plan_still_lacks_a_retail_epub_for():
    before = {"books": 10, "retail epub": 2, "converted epub": 6, "broken epub": 1, "no epub": 1,
              "no hardcover id": 0}
    after = dict(before, **{"retail epub": 3, "converted epub": 5})
    rows = [
        dict(cand(1), result="retail", ingested="a", available={"epub en": 2}),
        dict(cand(2, title="Dune"), result="no retail", available={"epub en": 3, "pdf en": 1}),
        dict(cand(3, kind="no epub", title="Atlas"), result="no epub", available={"pdf en": 2}),
        dict(cand(4, title="Ghost"), result="no hardcover id"),
        dict(cand(5, title="Unmerged"), result="retail", ingested="e", available={"epub en": 1}),
    ]
    still = [cand(2, title="Dune"), cand(3, kind="no epub", title="Atlas"), cand(4, title="Ghost"),
             cand(5, title="Unmerged"), cand(6, title="Untried")]
    md, fallback = cr.report(before, after, rows, still)
    assert "| retail epub | 2 | 3 | +1 |" in md
    assert "| retail | 2 |" in md and "| no epub | 1 |" in md and "| not tried | 1 |" in md
    assert "| retail copy not merged | 1 |" in md
    assert "| pdf en | 2 |" in md and "| epub en | 2 |" in md  # Formats found for Books still without one
    assert [(r["id"], r["result"]) for r in fallback] == [
        (2, "no retail"), (3, "no epub"), (4, "no hardcover id"), (5, "retail copy not merged"), (6, "not tried")]
    assert fallback[1] == {"id": 3, "title": "Atlas", "authors": "Andy Weir", "kind": "no epub",
                           "result": "no epub", "available": "pdf en 2"}
