"""Tests for truenas_drift.py. The Host is synthetic; they need no hops, sops or Host."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import truenas_drift as td  # noqa: E402

IDS = td.Identifiers({"addresses": {"host-v4": "192.0.2.10/24"}, "logins": {"operator": "alice"}, "names": {}})


def lines(r):
    return [IDS.redact(f"{f.where}: {f.message}") for f in r.drift]


def test_duplicate_key_is_an_error(tmp_path):
    f = tmp_path / "storage.yaml"
    f.write_text("datasets:\n  a/b: {}\n  a/b: {}\n")
    with pytest.raises(td.RecordError, match="duplicate key 'a/b' at line 3"):
        td.load_yaml(f)


def test_schedule_normalizes_names_numbers_and_omitted_fields():
    assert td.schedule({"minute": "00", "hour": "00", "dow": "7"}) == td.schedule({"minute": "0", "hour": "0", "dow": "sun"})
    assert td.schedule({"dow": "mon,tue"})["dow"] == "1,2"
    assert td.schedule({})["dom"] == "*"


def acl_entry(tag, perms, default=False, id=-1):
    return {"tag": tag, "id": id, "default": default,
            "perms": {"READ": "r" in perms, "WRITE": "w" in perms, "EXECUTE": "x" in perms}}


def posix_host(path, extra=()):
    base = [acl_entry("USER_OBJ", "rwx"), acl_entry("GROUP_OBJ", "rx"), acl_entry("OTHER", "")]
    return {"stat": {path: {"mode": 0o42750, "user": "alice", "group": "backups"}},
            "acl": {path: {"acltype": "POSIX1E", "acl": base + list(extra)}}}


def test_permissions_match_and_named_entries_are_drift():
    want = {"mode": "2750", "owner": "operator", "group": "backups"}
    r = td.Report()
    td.check_permissions("storage.yaml datasets.x", want, "/mnt/x", posix_host("/mnt/x"), IDS, r)
    assert r.drift == []

    host = posix_host("/mnt/x", [acl_entry("GROUP", "rx", id=1003), acl_entry("MASK", "rx")])
    td.check_permissions("storage.yaml datasets.x", want, "/mnt/x", host, IDS, r)
    assert lines(r) == ["storage.yaml datasets.x permissions acl: record [], Host [group:1003:r-x, mask::r-x]"]


def ident_host(users=(), groups=()):
    return {"user.query": [{"id": i, "builtin": False, **u} for i, u in enumerate(users, 100)],
            "group.query": [{"id": i, "builtin": False, "users": [], **g} for i, g in enumerate(groups, 200)],
            "api_key.query": []}


def test_unlisted_identities_are_drift_unless_in_identities_tsv():
    host = ident_host(users=[{"username": "plex", "uid": 4001}, {"username": "stray", "uid": 1099}],
                      groups=[{"group": "media", "gid": 1002}])
    r = td.Report()
    td.check_identities({}, host, IDS, r, ({"plex": 4001}, {"media": 1002, "gone": 1050}))
    assert lines(r) == ["identities.yaml users.stray: on the Host, not in the record",
                        "identities.tsv group gone: in the record, not on the Host"]


def smb_share(name, **kw):
    return {"name": name, "path": f"/mnt/{name}", "purpose": "DEFAULT_SHARE", "enabled": True, "browsable": True,
            "readonly": False, "options": {}, "audit": {"enable": False, "watch_list": []}, **kw}


def test_sharing_changed_extra_and_missing_shares_are_redacted():
    rec = {"smb": {"bindip": ["host-v4"]},
           "smb_shares": {"a": {"path": "/mnt/a", "readonly": True}, "b": {"path": "/mnt/b"}}}
    host = {"smb.config": {"bindip": ["192.0.2.99"]}, "nfs.config": {},
            "sharing.smb.query": [smb_share("a"), smb_share("c")], "sharing.nfs.query": []}
    r = td.Report()
    td.check_sharing(rec, host, IDS, r)
    out = lines(r)
    assert out == ["sharing.yaml smb bindip: record [<host-v4>], Host [192.0.2.99]",
                   "sharing.yaml smb_shares.a readonly: record true, Host false",
                   "sharing.yaml smb_shares.b: in the record, not on the Host",
                   "sharing.yaml smb_shares.c: on the Host, not in the record"]
    assert not any("192.0.2.10" in line or "alice" in line for line in out)


def test_main_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(td, "load_record", lambda: {})
    monkeypatch.setattr(td.Identifiers, "from_sops", classmethod(lambda cls: IDS))
    monkeypatch.setattr(td, "load_identities_tsv", lambda: ({}, {}))
    monkeypatch.setattr(td, "read_host", lambda record, ids: {})
    monkeypatch.setattr(td, "compare", lambda *a: td.Report())
    assert td.main([]) == 0
    assert capsys.readouterr().out == ""

    def drifted(*a):
        r = td.Report()
        r.diff("system.yaml general owner", "operator", "alice")
        return r
    monkeypatch.setattr(td, "compare", drifted)
    assert td.main([]) == 1
    assert capsys.readouterr().out == "system.yaml general owner: record operator, Host <operator>\n"

    def broken(record, ids):
        raise RuntimeError("hops truenas call pool.query failed")
    monkeypatch.setattr(td, "read_host", broken)
    assert td.main([]) == 2
