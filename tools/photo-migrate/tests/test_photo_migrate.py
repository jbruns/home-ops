"""Tests for photo_migrate.py. They need no Host, exiftool or Immich."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import photo_migrate as pm  # noqa: E402

CONFIG = pm.Config({
    "accounts": {"operator": "IMMICH_API_KEY_OPERATOR", "member": "IMMICH_API_KEY_MEMBER"},
    "default_owner": "operator",
    "owners": {"Member": "member", "Member Phone": "member"},
    "not_albums": ["Member", "Member Phone", "Operator Phone", "DSLR"],
    "timezone": "America/Los_Angeles",
    "apple_name_patterns": ["_iOS\\."],
    "utc_name_patterns": ["_iOS\\."],
})
TZ = CONFIG.tz


def test_folders_mapped_to_an_account_belong_to_it_and_all_others_to_the_default():
    assert pm.owner_of("Member Phone/2019/a.jpg", CONFIG) == "member"
    assert pm.owner_of("Member/a.jpg", CONFIG) == "member"
    assert pm.owner_of("Members Trip/a.jpg", CONFIG) == "operator"
    assert pm.owner_of("a.jpg", CONFIG) == "operator"


def rec(name, directory="/d/gf/Event 2015", **tags):
    """An exiftool -j -G1 -a -n record."""
    base = {"SourceFile": f"{directory}/{name}", "System:FileModifyDate": "2020:06:01 08:00:00-07:00", "File:MIMEType": "image/jpeg", "System:FileSize": 1000}
    base.update(tags)
    return base


def test_the_exif_capture_date_wins_over_the_filename_and_mtime_and_is_household_local_without_an_offset():
    r = rec("15-07-04 10-11-12 0001.jpg", **{"ExifIFD:DateTimeOriginal": "2015:07:04 21:30:00", "ExifIFD:CreateDate": "2015:07:05 00:00:00"})
    assert pm.resolve_date(r, TZ) == ("2015-07-04T21:30:00-07:00", "exif", False)


def test_an_exif_offset_tag_gives_its_date_the_offset():
    r = rec("a.jpg", **{"ExifIFD:DateTimeOriginal": "2021:03:04 05:06:07", "ExifIFD:OffsetTimeOriginal": "+09:00",
                        "ExifIFD:OffsetTime": "+01:00"})
    assert pm.resolve_date(r, TZ) == ("2021-03-04T05:06:07+09:00", "exif", False)
    r = rec("a.jpg", **{"ExifIFD:CreateDate": "2021:03:04 05:06:07", "ExifIFD:OffsetTimeDigitized": "-03:00"})
    assert pm.resolve_date(r, TZ) == ("2021-03-04T05:06:07-03:00", "exif", False)
    # OffsetTime belongs to ModifyDate, not the capture date: the capture date is household-local
    r = rec("a.jpg", **{"ExifIFD:DateTimeOriginal": "2021:03:04 05:06:07", "ExifIFD:OffsetTime": "+01:00"})
    assert pm.resolve_date(r, TZ) == ("2021-03-04T05:06:07-08:00", "exif", False)


def test_an_exif_offset_tag_never_shifts_a_bare_quicktime_date_which_stays_utc():
    r = rec("a.mov", **{"QuickTime:CreateDate": "2019:01:02 03:04:05", "ExifIFD:OffsetTimeDigitized": "-08:00",
                        "ExifIFD:OffsetTimeOriginal": "-08:00", "ExifIFD:OffsetTime": "-08:00"})
    assert pm.resolve_date(r, TZ) == ("2019-01-02T03:04:05+00:00", "exif", False)


def test_a_quicktime_creation_date_keeps_its_offset_and_a_bare_quicktime_date_is_utc():
    r = rec("IMG_1.MOV", **{"File:MIMEType": "video/quicktime", "Keys:CreationDate": "2019:01:02 03:04:05-08:00",
                            "QuickTime:CreateDate": "2019:01:02 11:04:05"})
    assert pm.resolve_date(r, TZ) == ("2019-01-02T03:04:05-08:00", "exif", False)
    r = rec("clip.mp4", **{"File:MIMEType": "video/mp4", "QuickTime:CreateDate": "2019:01:02 03:04:05"})
    assert pm.resolve_date(r, TZ) == ("2019-01-02T03:04:05+00:00", "exif", False)


def test_a_bare_quicktime_date_matching_the_filename_clock_to_the_minute_is_household_local():
    r = rec("vid-20140808-224025271.mp4", **{"QuickTime:CreateDate": "2014:08:08 22:40:39"})
    assert pm.resolve_date(r, TZ) == ("2014-08-08T22:40:39-07:00", "quicktime-local", False)
    r = rec("20150704_221912.mp4", **{"QuickTime:CreateDate": "2015:07:04 22:19:59"})
    assert pm.resolve_date(r, TZ)[:2] == ("2015-07-04T22:19:59-07:00", "quicktime-local")


def test_a_bare_quicktime_date_stays_utc_unless_the_filename_clock_matches_or_when_turned_off():
    utc = rec("vid-20140808-224025271.mp4", **{"QuickTime:CreateDate": "2014:08:09 05:40:39"})
    assert pm.resolve_date(utc, TZ) == ("2014-08-09T05:40:39+00:00", "exif", False)
    next_minute = rec("20150704_221912.mp4", **{"QuickTime:CreateDate": "2015:07:04 22:20:00"})
    assert pm.resolve_date(next_minute, TZ)[1] == "exif"
    unnamed = rec("MVI_0266.MOV", **{"QuickTime:CreateDate": "2012:05:29 18:54:01"})
    assert pm.resolve_date(unnamed, TZ)[:2] == ("2012-05-29T18:54:01+00:00", "exif")
    local = rec("vid-20140808-224025271.mp4", **{"QuickTime:CreateDate": "2014:08:08 22:40:39"})
    assert pm.resolve_date(local, TZ, quicktime_local=False) == ("2014-08-08T22:40:39+00:00", "exif", False)
    assert pm.Config({"accounts": {}, "default_owner": "operator"}).quicktime_local is True
    assert pm.Config({"accounts": {}, "default_owner": "operator", "quicktime_local_when_name_matches": False}).quicktime_local is False


def test_a_name_matching_a_utc_pattern_carries_utc_and_never_makes_its_quicktime_date_local():
    utc = CONFIG.utc_names
    assert pm.resolve_date(rec("20170116_213232000_iOS.jpg"), TZ, utc_names=utc) == (
        "2017-01-16T21:32:32+00:00", "filename", False)
    r = rec("20160711_011706000_iOS.mp4", **{"QuickTime:CreateDate": "2016:07:11 01:17:06"})
    assert pm.resolve_date(r, TZ, utc_names=utc) == ("2016-07-11T01:17:06+00:00", "exif", False)
    r = rec("20160711_011706.mp4", **{"QuickTime:CreateDate": "2016:07:11 01:17:06"})
    assert pm.resolve_date(r, TZ, utc_names=utc)[1] == "quicktime-local"
    assert planned(rec("20170116_213232000_iOS.jpg"))["Event 2015/20170116_213232000_iOS.jpg"]["date"] == (
        "2017-01-16T21:32:32+00:00")


def test_zero_or_garbage_exif_dates_are_ignored_and_mtime_is_shown_household_local():
    r = rec("clip.mov", **{"QuickTime:CreateDate": "0000:00:00 00:00:00", "ExifIFD:DateTimeOriginal": "    :  :     :  :  ",
                           "System:FileModifyDate": "2020:06:01 15:00:00+00:00"})
    assert pm.resolve_date(r, TZ) == ("2020-06-01T08:00:00-07:00", "mtime", False)


def test_the_filename_dates_a_file_without_exif_in_household_time_with_daylight_saving():
    assert pm.resolve_date(rec("15-07-04 10-11-12 0001.jpg"), TZ) == ("2015-07-04T10:11:12-07:00", "filename", False)
    assert pm.resolve_date(rec("2004-10-29 22-21-42 0791.JPG"), TZ) == ("2004-10-29T22:21:42-07:00", "filename", False)
    assert pm.resolve_date(rec("98-01-02 03-04-05 0001.jpg"), TZ) == ("1998-01-02T03:04:05-08:00", "filename", False)


def test_an_ambiguous_local_time_takes_its_first_occurrence_and_is_marked_uncertain():
    # 2015-11-01 01:30 happens twice in the household's zone: first in daylight time
    assert pm.resolve_date(rec("15-11-01 01-30-00 0001.jpg"), TZ) == ("2015-11-01T01:30:00-07:00", "filename", True)
    r = rec("a.jpg", **{"ExifIFD:DateTimeOriginal": "2015:11:01 01:30:00"})
    assert pm.resolve_date(r, TZ) == ("2015-11-01T01:30:00-07:00", "exif", True)


def test_a_nonexistent_local_time_moves_to_the_instant_after_the_transition_and_is_marked_uncertain():
    # 2015-03-08 02:30 never happens in the household's zone: clocks jump from 02:00 to 03:00
    assert pm.resolve_date(rec("15-03-08 02-30-00 0001.jpg"), TZ) == ("2015-03-08T03:00:00-07:00", "filename", True)


def test_a_date_with_its_own_offset_is_never_uncertain():
    r = rec("a.jpg", **{"ExifIFD:DateTimeOriginal": "2015:11:01 01:30:00", "ExifIFD:OffsetTimeOriginal": "-08:00"})
    assert pm.resolve_date(r, TZ) == ("2015-11-01T01:30:00-08:00", "exif", False)


def test_common_camera_and_export_filename_dates_are_read_too():
    assert pm.resolve_date(rec("IMG_20160524_151629.jpg"), TZ) == ("2016-05-24T15:16:29-07:00", "filename", False)
    assert pm.resolve_date(rec("20170116_213232000_iOS.jpg"), TZ) == ("2017-01-16T21:32:32-08:00", "filename", False)
    assert pm.resolve_date(rec("Photo-2017-03-01-12-16-28_2120.JPG"), TZ) == ("2017-03-01T12:16:28-08:00", "filename", False)
    assert pm.resolve_date(rec("FB_IMG_1445486645351.jpg"), TZ)[1] == "mtime"


def test_an_impossible_filename_date_falls_through_to_mtime():
    assert pm.resolve_date(rec("15-13-40 10-11-12 0001.jpg"), TZ) == ("2020-06-01T08:00:00-07:00", "mtime", False)


def test_an_exif_offset_and_subseconds_are_kept():
    r = rec("a.jpg", **{"ExifIFD:DateTimeOriginal": "2021:03:04 05:06:07.123+01:00"})
    assert pm.resolve_date(r, TZ) == ("2021-03-04T05:06:07.123+01:00", "exif", False)


def test_a_top_level_event_folder_is_one_album_including_its_subfolders():
    tops = {"Wedding 2010"}
    assert pm.album_of("Wedding 2010/a.jpg", CONFIG, tops) == "Wedding 2010"
    assert pm.album_of("Wedding 2010/Not used/b.jpg", CONFIG, tops) == "Wedding 2010"


def test_a_folder_holding_only_event_folders_makes_one_album_per_child():
    assert pm.album_of("Santa Photos/Santa 2019/a.jpg", CONFIG, set()) == "Santa Photos / Santa 2019"
    assert pm.album_of("Santa Photos/Santa 2019/deep/a.jpg", CONFIG, set()) == "Santa Photos / Santa 2019"


def test_device_and_person_folders_and_the_root_are_never_albums():
    assert pm.album_of("Member Phone/a.jpg", CONFIG, {"Member Phone"}) is None
    assert pm.album_of("Member/2014/Birthday/a.jpg", CONFIG, set()) is None
    assert pm.album_of("a.jpg", CONFIG, set()) is None

ROOT = "/d/gf"


def planned(*records):
    return {r["path"]: r for r in pm.plan(list(records), ROOT, CONFIG)}


def test_a_non_apple_file_is_planned_for_upload_to_its_owner_and_album():
    rows = planned(rec("IMG_1.JPG", "/d/gf/Member Phone", **{"IFD0:Make": "samsung", "ExifIFD:DateTimeOriginal": "2019:01:02 03:04:05"}),
                   rec("DSC_2.JPG", "/d/gf/Event 2015", **{"IFD0:Make": "NIKON"}))
    assert rows["Member Phone/IMG_1.JPG"] == {
        "path": "Member Phone/IMG_1.JPG", "action": "upload", "reason": None, "owner": "member", "album": None,
        "media": "image", "date": "2019-01-02T03:04:05-08:00", "date_source": "exif", "date_uncertain": False, "make": "samsung",
        "size": 1000, "pair": None}
    assert rows["Event 2015/DSC_2.JPG"]["album"] == "Event 2015"
    assert rows["Event 2015/DSC_2.JPG"]["owner"] == "operator"


def test_apple_stills_and_videos_are_skipped_by_their_make_wherever_they_are():
    rows = planned(rec("IMG_1.HEIC", "/d/gf/DSLR", **{"File:MIMEType": "image/heic", "IFD0:Make": "Apple"}),
                   rec("IMG_2.MOV", "/d/gf/Event 2015", **{"File:MIMEType": "video/quicktime", "Keys:Make": "Apple"}))
    assert rows["DSLR/IMG_1.HEIC"]["action"] == "skip"
    assert rows["DSLR/IMG_1.HEIC"]["reason"] == "apple"
    assert rows["Event 2015/IMG_2.MOV"]["reason"] == "apple"
    assert rows["Event 2015/IMG_2.MOV"]["media"] == "video"


def test_a_live_photo_video_is_skipped_with_its_apple_still_whatever_its_own_make():
    rows = planned(rec("IMG_1.JPG", **{"IFD0:Make": "Apple"}), rec("img_1.mov", **{"File:MIMEType": "video/quicktime"}),
                   rec("DSC_2.JPG", **{"IFD0:Make": "Canon"}), rec("DSC_2.MOV", **{"File:MIMEType": "video/quicktime"}))
    assert rows["Event 2015/img_1.mov"]["reason"] == "apple-live"
    assert rows["Event 2015/img_1.mov"]["pair"] == "Event 2015/IMG_1.JPG"
    assert rows["Event 2015/DSC_2.MOV"]["action"] == "upload"


def test_any_spelling_of_apple_as_make_is_apple_and_takes_its_live_video_with_it():
    rows = planned(rec("IMG_1.JPG", **{"IFD0:Make": " Apple Inc. "}), rec("IMG_1.MOV", **{"File:MIMEType": "video/quicktime"}),
                   rec("IMG_2.JPG", **{"IFD0:Make": "APPLE"}), rec("IMG_3.MOV", **{"File:MIMEType": "video/quicktime", "Keys:Make": "Apple Computer"}),
                   rec("IMG_4.JPG", **{"IFD0:Make": "Pineapple Cameras"}))
    assert rows["Event 2015/IMG_1.JPG"]["reason"] == "apple"
    assert rows["Event 2015/IMG_1.MOV"]["reason"] == "apple-live"
    assert rows["Event 2015/IMG_2.JPG"]["reason"] == "apple"
    assert rows["Event 2015/IMG_3.MOV"]["reason"] == "apple"
    assert rows["Event 2015/IMG_4.JPG"]["action"] == "upload"


def test_a_file_with_no_make_named_as_an_ios_export_is_held_as_apple_with_its_live_video():
    rows = planned(rec("20170116_213232000_iOS.jpg"), rec("20170116_213232000_iOS.MOV", **{"File:MIMEType": "video/quicktime"}),
                   rec("20170116_213233000_iOS.jpg", **{"IFD0:Make": "samsung"}), rec("kiosk_iOS_guide.jpg"))
    assert rows["Event 2015/20170116_213232000_iOS.jpg"]["reason"] == "apple"
    assert rows["Event 2015/20170116_213232000_iOS.MOV"]["reason"] == "apple-live"
    assert rows["Event 2015/20170116_213233000_iOS.jpg"]["action"] == "upload"
    assert rows["Event 2015/kiosk_iOS_guide.jpg"]["action"] == "upload"


def test_an_apple_file_held_for_reconciliation_carries_its_date_uncertainty():
    rows = planned(rec("IMG_1.HEIC", **{"IFD0:Make": "Apple", "ExifIFD:DateTimeOriginal": "2015:11:01 01:30:00"}))
    assert rows["Event 2015/IMG_1.HEIC"]["reason"] == "apple"
    assert rows["Event 2015/IMG_1.HEIC"]["date_uncertain"] is True


def test_files_exiftool_cannot_read_or_that_are_not_media_are_skipped():
    rows = planned(rec("broken.jpg", **{"ExifTool:Error": "File is empty", "File:MIMEType": None}),
                   rec("ZbThumbnail.info", **{"ExifTool:Error": "Unknown file type", "File:MIMEType": None}),
                   rec("MVI_1.THM", **{"IFD0:Make": "Canon"}),
                   rec("notes.jpg", **{"File:MIMEType": "text/plain"}))
    assert rows["Event 2015/broken.jpg"]["reason"] == "unreadable"
    assert rows["Event 2015/ZbThumbnail.info"]["reason"] == "not-media"
    assert rows["Event 2015/MVI_1.THM"]["reason"] == "not-media"
    assert rows["Event 2015/notes.jpg"]["reason"] == "not-media"


# --- Upload and albums, against a fake Immich ---

import hashlib  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import pytest  # noqa: E402

KEYS = {"IMMICH_API_KEY_OPERATOR": "k-op", "IMMICH_API_KEY_MEMBER": "k-mem"}


class FakeImmich:
    """Just enough of Immich's API: per-user checksum dedup, albums, album users."""

    def __init__(self):
        self.users = {"k-op": str(uuid.uuid4()), "k-mem": str(uuid.uuid4())}
        self.assets = {}  # (user, sha1) -> asset
        self.albums = {}  # id -> album
        self.uploads = []
        self.trashed = set()  # asset ids in the trash
        self.forbidden = set()  # asset ids an album add refuses
        self.drop_album_creates = 0  # create the album but lose the response this many times
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def body(self):
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

            def do_GET(self):
                user = fake.users[self.headers["x-api-key"]]
                if self.path == "/api/users/me":
                    return self.reply(200, {"id": user})
                if self.path.startswith("/api/albums?"):
                    return self.reply(200, [fake.album_dto(a) for a in fake.albums.values() if a["owner"] == user])
                self.reply(404, {})

            def do_POST(self):
                user = fake.users[self.headers["x-api-key"]]
                if self.path == "/api/assets/bulk-upload-check":
                    results = []
                    for item in json.loads(self.body())["assets"]:
                        a = fake.assets.get((user, item["checksum"]))
                        results.append({"id": item["id"], "action": "reject", "reason": "duplicate", "assetId": a["id"],
                                        "isTrashed": a["id"] in fake.trashed} if a
                                       else {"id": item["id"], "action": "accept"})
                    return self.reply(200, {"results": results})
                if self.path == "/api/assets":
                    raw = self.body()
                    boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
                    fields = {}
                    for part in raw.split(b"--" + boundary)[1:-1]:
                        head, _, value = part[2:-2].partition(b"\r\n\r\n")
                        fields[head.split(b'name="')[1].split(b'"')[0].decode()] = value
                    sha1 = hashlib.sha1(fields["assetData"]).hexdigest()
                    fake.uploads.append({k: v if k == "assetData" else v.decode() for k, v in fields.items()})
                    if (user, sha1) in fake.assets:
                        return self.reply(200, {"id": fake.assets[(user, sha1)]["id"], "status": "duplicate"})
                    a = fake.assets[(user, sha1)] = {"id": str(uuid.uuid4())}
                    return self.reply(201, {"id": a["id"], "status": "created"})
                if self.path == "/api/albums":
                    dto = json.loads(self.body())
                    a = {"id": str(uuid.uuid4()), "owner": user, "name": dto["albumName"], "assets": [],
                         "users": {u["userId"]: u["role"] for u in dto.get("albumUsers", [])}}
                    fake.albums[a["id"]] = a
                    if fake.drop_album_creates:
                        fake.drop_album_creates -= 1
                        self.close_connection = True
                        return
                    return self.reply(201, fake.album_dto(a))
                self.reply(404, {})

            def do_PUT(self):
                album = fake.albums[self.path.split("/")[3]]
                dto = json.loads(self.body())
                if self.path.endswith("/assets"):
                    out = []
                    for i in dto["ids"]:
                        if i in fake.forbidden:
                            out.append({"id": i, "success": False, "error": "no_permission"})
                            continue
                        out.append({"id": i, "success": i not in album["assets"]} | ({} if i not in album["assets"] else {"error": "duplicate"}))
                        if i not in album["assets"]:
                            album["assets"].append(i)
                    return self.reply(200, out)
                for u in dto["albumUsers"]:
                    album["users"][u["userId"]] = u["role"]
                self.reply(200, fake.album_dto(album))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def album_dto(self, a):
        return {"id": a["id"], "albumName": a["name"], "albumUsers": [{"user": {"id": u}, "role": r} for u, r in a["users"].items()]}


@pytest.fixture
def immich():
    fake = FakeImmich()
    yield fake
    fake.server.shutdown()


def write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def row(path, owner="operator", album=None, action="upload", date="2015-07-04T10:11:12", source="filename"):
    return {"path": path, "action": action, "reason": None if action == "upload" else "apple", "owner": owner, "album": album,
            "media": "image", "date": date, "date_source": source, "date_uncertain": False, "make": None, "size": 1,
            "pair": None}


def migrate(immich, root, rows, journal=()):
    clients = pm.clients(CONFIG, immich.url, KEYS)
    done = list(pm.upload(rows, str(root), clients, journal, workers=2))
    albums = list(pm.sync_albums(rows, journal + tuple(done), clients))
    return done, albums


def test_upload_sends_each_planned_file_to_its_owner_with_the_resolved_date(tmp_path, immich):
    write(tmp_path, "Event/a.jpg", b"a")
    write(tmp_path, "Member/b.jpg", b"b")
    write(tmp_path, "Event/skipped.jpg", b"s")
    rows = [row("Event/a.jpg"), row("Member/b.jpg", owner="member", date="2020-06-01T08:00:00-07:00", source="mtime"),
            row("Event/skipped.jpg", action="skip")]
    done, _ = migrate(immich, tmp_path, rows)
    assert sorted((d["path"], d["status"]) for d in done) == [("Event/a.jpg", "created"), ("Member/b.jpg", "created")]
    assert {(u["filename"], u["fileCreatedAt"]) for u in immich.uploads} == {
        ("a.jpg", "2015-07-04T10:11:12Z"), ("b.jpg", "2020-06-01T08:00:00-07:00")}
    owners = {user for user, _ in immich.assets}
    assert owners == {immich.users["k-op"], immich.users["k-mem"]}


def test_a_rerun_uploads_nothing_twice(tmp_path, immich):
    write(tmp_path, "Event/a.jpg", b"a")
    write(tmp_path, "Event/b.jpg", b"b")
    rows = [row("Event/a.jpg", album="Event"), row("Event/b.jpg", album="Event")]
    first, _ = migrate(immich, tmp_path, rows[:1])
    # Resumed with the journal: the journalled file is not touched; lost journal: Immich reports a duplicate.
    again, _ = migrate(immich, tmp_path, rows, tuple(first))
    assert [(d["path"], d["status"]) for d in again] == [("Event/b.jpg", "created")]
    lost, _ = migrate(immich, tmp_path, rows)
    assert sorted(d["status"] for d in lost) == ["duplicate", "duplicate"]
    assert len(immich.uploads) == 2
    assert len(immich.assets) == 2


def test_each_event_album_is_created_once_by_its_owner_and_shared_as_viewer_with_the_other_account(tmp_path, immich):
    write(tmp_path, "Event/a.jpg", b"a")
    write(tmp_path, "Event/b.jpg", b"b")
    rows = [row("Event/a.jpg", album="Event"), row("Event/b.jpg", album="Event"), row("Loose/c.jpg")]
    write(tmp_path, "Loose/c.jpg", b"c")
    migrate(immich, tmp_path, rows)
    migrate(immich, tmp_path, rows)
    (album,) = immich.albums.values()
    assert album["name"] == "Event"
    assert album["owner"] == immich.users["k-op"]
    assert album["users"] == {immich.users["k-mem"]: "viewer"}
    assert len(album["assets"]) == 2


def test_a_missing_file_is_journalled_as_an_error_and_the_rest_continue(tmp_path, immich):
    write(tmp_path, "Event/a.jpg", b"a")
    done, _ = migrate(immich, tmp_path, [row("Event/gone.jpg"), row("Event/a.jpg")])
    status = {d["path"]: d["status"] for d in done}
    assert status == {"Event/gone.jpg": "error", "Event/a.jpg": "created"}


def test_the_report_counts_each_accounts_uploads_skips_date_sources_and_albums():
    rows = [row("Event/a.jpg", album="Event"), row("Event/b.jpg", album="Event", source="exif"),
            row("Member/c.jpg", owner="member"),
            {**row("Event/d.heic"), "action": "skip", "reason": "apple", "make": "Apple"},
            {**row("Event/f_iOS.jpg"), "action": "skip", "reason": "apple", "make": None},
            {**row("Event/d.mov"), "action": "skip", "reason": "apple-live", "media": "video"},
            {**row("Event/e.jpg"), "action": "skip", "reason": "unreadable"}]
    text = pm.report(rows)
    assert "operator  upload 2  skip 4 (apple stills 2 (by name 1), apple videos 0, apple live videos 1, not-media 0, unsupported 0, unreadable 1)" in text
    assert "member  upload 1  skip 0" in text
    assert "operator  dates: exif 1, quicktime-local 0, filename 1, mtime 0; uncertain 0 to upload, 0 held" in text
    assert "operator  albums 1 (2 files)" in text
    assert "unreadable: Event/e.jpg" in text


def test_media_whose_extension_immich_does_not_take_is_skipped_as_unsupported():
    rows = planned(rec("Card.pub", **{"File:MIMEType": "image/vnd.fpx"}), rec("Cheers"), rec("ok.JPEG"))
    assert rows["Event 2015/Card.pub"]["reason"] == "unsupported"
    assert rows["Event 2015/Cheers"]["reason"] == "unsupported"
    assert rows["Event 2015/ok.JPEG"]["action"] == "upload"


def test_a_file_whose_match_is_in_the_trash_is_an_error_to_restore_and_is_rechecked_next_run(tmp_path, immich):
    write(tmp_path, "Event/a.jpg", b"a")
    rows = [row("Event/a.jpg", album="Event")]
    first, _ = migrate(immich, tmp_path, rows)
    immich.trashed.add(first[0]["id"])
    second, albums = migrate(immich, tmp_path, rows)  # a lost journal: Immich's match is found, in the trash
    assert second[0]["status"] == "trashed"
    assert "restore" in second[0]["error"]
    third, _ = migrate(immich, tmp_path, rows, tuple(second))
    assert [d["status"] for d in third] == ["trashed"]


def test_every_album_add_failure_other_than_a_duplicate_is_recorded(tmp_path, immich):
    write(tmp_path, "Event/a.jpg", b"a")
    write(tmp_path, "Event/b.jpg", b"b")
    rows = [row("Event/a.jpg", album="Event"), row("Event/b.jpg", album="Event")]
    done, _ = migrate(immich, tmp_path, rows)
    immich.forbidden.add(done[1]["id"])
    _, (album,) = migrate(immich, tmp_path, rows, tuple(done))
    assert album["failed"] == [{"id": done[1]["id"], "error": "no_permission"}]
    assert album["error"] is None


def test_a_lost_album_create_response_does_not_create_a_second_album(tmp_path, immich):
    write(tmp_path, "Event/a.jpg", b"a")
    immich.drop_album_creates = 1
    _, (album,) = migrate(immich, tmp_path, [row("Event/a.jpg", album="Event")])
    assert len(immich.albums) == 1
    assert album["error"] is None
    assert len(next(iter(immich.albums.values()))["assets"]) == 1


def test_upload_exits_nonzero_when_an_album_fails(tmp_path, immich, monkeypatch, capsys):
    import io
    data = tmp_path / "data"
    write(data, "Event/a.jpg", b"a")
    manifest, journal, config = tmp_path / "m.jsonl", tmp_path / "j.jsonl", tmp_path / "c.json"
    manifest.write_text(json.dumps(row("Event/a.jpg", album="Event")) + "\n")
    config.write_text(json.dumps({"accounts": CONFIG.accounts, "default_owner": "operator"}))
    args = ["upload", "--config", str(config), "--manifest", str(manifest), "--journal", str(journal),
            "--root", str(data), "--server", immich.url]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{k}={v}\n" for k, v in KEYS.items())))
    assert pm.main(args) == 0
    journal.write_text(capsys.readouterr().out)
    immich.forbidden.add(json.loads(journal.read_text())["id"])
    next(iter(immich.albums.values()))["assets"].clear()
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{k}={v}\n" for k, v in KEYS.items())))
    assert pm.main(args) == 1


def test_a_journal_cut_off_mid_record_resumes_and_rechecks_that_file(tmp_path, immich):
    write(tmp_path, "Event/a.jpg", b"a")
    write(tmp_path, "Event/b.jpg", b"b")
    write(tmp_path, "Event/c.jpg", b"c")
    rows = [row("Event/a.jpg"), row("Event/b.jpg"), row("Event/c.jpg")]
    done, _ = migrate(immich, tmp_path, rows)
    a, b, c = (json.dumps(d) for d in done)
    path = tmp_path / "journal.jsonl"
    path.write_text(a + "\n" + b[:30] + c + "\n" + b[:20])  # a cut-off write, then later rows appended to it
    journal = pm.read_jsonl(str(path))
    assert [j["path"] for j in journal] == ["Event/a.jpg", "Event/c.jpg"]
    again, _ = migrate(immich, tmp_path, rows, tuple(journal))
    assert [(d["path"], d["status"]) for d in again] == [("Event/b.jpg", "duplicate")]
    assert len(immich.uploads) == 3


def test_the_report_counts_uncertain_dates_of_uploads_and_held_files():
    rows = [{**row("Event/a.jpg"), "date_uncertain": True},
            {**row("Event/b.heic"), "action": "skip", "reason": "apple", "date_uncertain": True},
            {**row("Event/c.heic"), "action": "skip", "reason": "apple"}]
    assert "uncertain 1 to upload, 1 held" in pm.report(rows)


def test_a_file_journalled_as_done_under_another_owner_is_an_error_and_never_complete(tmp_path, immich):
    write(tmp_path, "Event/a.jpg", b"a")
    first, _ = migrate(immich, tmp_path, [row("Event/a.jpg", album="Event")])
    again, albums = migrate(immich, tmp_path, [row("Event/a.jpg", owner="member", album="Event")], tuple(first))
    assert [(d["path"], d["owner"], d["status"]) for d in again] == [("Event/a.jpg", "member", "owner-mismatch")]
    assert "operator" in again[0]["error"]
    assert albums == [] and len(immich.uploads) == 1
    # still not complete on the next run
    third, _ = migrate(immich, tmp_path, [row("Event/a.jpg", owner="member")], tuple(first) + tuple(again))
    assert [d["status"] for d in third] == ["owner-mismatch"]


def upload_args(tmp_path, immich, manifest_text, monkeypatch):
    import io
    manifest, journal, config = tmp_path / "m.jsonl", tmp_path / "j.jsonl", tmp_path / "c.json"
    manifest.write_text(manifest_text)
    config.write_text(json.dumps({"accounts": CONFIG.accounts, "default_owner": "operator"}))
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{k}={v}\n" for k, v in KEYS.items())))
    return ["upload", "--config", str(config), "--manifest", str(manifest), "--journal", str(journal),
            "--root", str(tmp_path), "--server", immich.url]


def test_upload_exits_nonzero_on_an_owner_mismatch(tmp_path, immich, monkeypatch, capsys):
    write(tmp_path, "Event/a.jpg", b"a")
    args = upload_args(tmp_path, immich, json.dumps(row("Event/a.jpg")) + "\n", monkeypatch)
    assert pm.main(args) == 0
    (tmp_path / "j.jsonl").write_text(capsys.readouterr().out)
    args = upload_args(tmp_path, immich, json.dumps(row("Event/a.jpg", owner="member")) + "\n", monkeypatch)
    assert pm.main(args) == 1


def test_a_malformed_manifest_record_stops_the_upload_before_anything_is_sent(tmp_path, immich, monkeypatch):
    import pytest
    write(tmp_path, "Event/a.jpg", b"a")
    write(tmp_path, "Event/b.jpg", b"b")
    text = json.dumps(row("Event/a.jpg")) + "\n" + json.dumps(row("Event/b.jpg"))[:40] + "\n"
    with pytest.raises(SystemExit, match=r"m\.jsonl:2"):
        pm.main(upload_args(tmp_path, immich, text, monkeypatch))
    assert immich.uploads == []
