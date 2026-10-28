#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Report every difference between the Host Configuration record (host/truenas/) and the Host.

Reads the record, decrypts identifiers.sops.yaml to turn roles into values, reads the Host through
`hops truenas call --json` (read-only), and prints one line per difference. Prints nothing and exits 0
when they match; exits 1 on drift, 2 on error. Values are printed by role, never as addresses or logins.

Checks follow host/truenas/README.md: every listed setting is compared, nothing unlisted is compared
against TrueNAS's defaults, and lists are complete: an item on the Host that the record doesn't list is drift.
Settings hops redacts or cannot read are not compared; `--unchecked` lists them.
"""
import argparse
import json
import re
import subprocess
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
RECORD = REPO / "host" / "truenas"
TSV = REPO / "host" / "identities.tsv"


class RecordError(Exception):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    """A SafeLoader that refuses a mapping with the same key twice (YAML keeps the last one silently)."""


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise RecordError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_yaml(path):
    try:
        return yaml.load(Path(path).read_text(), Loader=UniqueKeyLoader) or {}
    except RecordError as e:
        raise RecordError(f"{Path(path).name}: {e}") from None


def load_record(directory=RECORD):
    names = ("storage", "sharing", "system", "services", "identities", "network")
    return {n: load_yaml(Path(directory) / f"{n}.yaml") for n in names}


def load_identities_tsv(path=TSV):
    users, groups = {}, {}
    for line in Path(path).read_text().splitlines():
        f = line.split("#", 1)[0].split()
        if f and f[0] == "group":
            groups[f[1]] = int(f[2])
        elif f and f[0] == "user":
            users[f[1]] = int(f[2])
    return users, groups


class Identifiers:
    """Roles from identifiers.sops.yaml. Values are only compared and redacted, never printed."""

    def __init__(self, data):
        self.addresses = dict(data.get("addresses", {}))
        self.logins = dict(data.get("logins", {}))
        self.names = dict(data.get("names", {}))

    @classmethod
    def from_sops(cls, path=RECORD / "identifiers.sops.yaml"):
        out = subprocess.run(["sops", "-d", str(path)], capture_output=True, text=True)
        if out.returncode:
            raise RecordError(f"sops could not decrypt {path.name}")
        return cls(yaml.safe_load(out.stdout))

    def address(self, ref):
        """{address: role} -> the value, without a prefix length."""
        role = ref["address"] if isinstance(ref, dict) else ref
        if role not in self.addresses:
            raise RecordError(f"unknown address role {role!r}")
        return str(self.addresses[role]).split("/")[0]

    def cidr(self, ref):
        role = ref["address"] if isinstance(ref, dict) else ref
        return str(self.addresses[role])

    def name(self, ref):
        role = ref["name"] if isinstance(ref, dict) else ref
        if role not in self.names:
            raise RecordError(f"unknown name role {role!r}")
        return str(self.names[role])

    def login(self, name):
        """The record writes the Operator's login and group as `operator`, and `<operator>` in paths."""
        if name is None:
            return None
        op = self.logins.get("operator", "operator")
        return op if name == "operator" else str(name).replace("<operator>", op)

    def redact(self, text):
        pairs = []
        for role, v in self.addresses.items():
            pairs += [(str(v), f"<{role}>"), (str(v).split("/")[0], f"<{role}>")]
        pairs += [(str(v), f"<{role}>") for role, v in self.names.items()]
        pairs += [(str(v), "<operator>") for v in self.logins.values()]
        for value, role in sorted(pairs, key=lambda p: -len(p[0])):
            if value:
                text = text.replace(value, role)
        return text


@dataclass
class Finding:
    where: str
    message: str


class Report:
    def __init__(self):
        self.drift, self.unchecked = [], []

    def diff(self, where, record, host):
        if norm(record) != norm(host):
            self.drift.append(Finding(where, f"record {fmt(record)}, Host {fmt(host)}"))

    def missing(self, where):
        self.drift.append(Finding(where, "in the record, not on the Host"))

    def extra(self, where):
        self.drift.append(Finding(where, "on the Host, not in the record"))

    def skip(self, where, reason):
        self.unchecked.append(Finding(where, reason))


def norm(v):
    if isinstance(v, (list, tuple)) and all(isinstance(x, (str, int, float, bool)) or x is None for x in v):
        return sorted(v, key=str)
    if isinstance(v, str) and v.upper() in {"TRUE", "FALSE"}:
        return v.upper() == "TRUE"
    if isinstance(v, str):
        return v.upper() if re.fullmatch(r"[A-Za-z0-9_]+", v) else v
    return v


def fmt(v):
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(fmt(x) for x in v) + "]"
    if v is None:
        return "none"
    if isinstance(v, bool):
        return str(v).lower()
    return str(v)


SIZES = {"": 1, "B": 1, "K": 1024, "KIB": 1024, "M": 1024**2, "MIB": 1024**2, "G": 1024**3, "GIB": 1024**3,
         "T": 1024**4, "TIB": 1024**4}


def size(v):
    if v in (None, 0, "0", "none"):
        return None
    m = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]*)\s*", str(v))
    if not m or m.group(2).upper() not in SIZES:
        raise RecordError(f"unreadable size {v!r}")
    return int(float(m.group(1)) * SIZES[m.group(2).upper()])


DOW = {"sun": "0", "mon": "1", "tue": "2", "wed": "3", "thu": "4", "fri": "5", "sat": "6", "7": "0"}


def schedule(s):
    """A cron-style schedule, with omitted fields `*` and numbers and weekday names normalized."""
    out = {}
    for k in ("minute", "hour", "dom", "month", "dow"):
        parts = []
        for p in str((s or {}).get(k, "*")).lower().split(","):
            p = p.strip()
            if k == "dow":
                p = DOW.get(p, p)
            parts.append(str(int(p)) if p.isdigit() else p)
        out[k] = ",".join(sorted(parts))
    return out


def by(rows, key):
    return {r[key]: r for r in rows}


def prop(ds, name):
    v = ds.get(name)
    return v.get("value") if isinstance(v, dict) else v


# ---- storage -------------------------------------------------------------------------------------------------

def check_storage(rec, host, ids, r):
    pools = by(host["pool.query"], "name")
    for name, p in rec.get("pools", {}).items():
        w = f"storage.yaml pools.{name}"
        if name == "boot-pool":
            r.skip(w, "boot-pool is not in pool.query, and hops has no boot environment read")
            continue
        if name not in pools:
            r.missing(w)
            continue
        hp = pools[name]
        if "autotrim" in p:
            r.diff(f"{w} autotrim", p["autotrim"], prop(hp, "autotrim") in ("on", "ON", True))
        topo = p.get("topology", {})
        for cls, vdevs in hp["topology"].items():
            want = topo.get(cls)
            if cls == "spare":
                r.diff(f"{w} topology.spare", want or 0, len(vdevs))
                continue
            have = {"vdevs": len(vdevs), "type": vdevs[0]["type"], "width": len(vdevs[0]["children"]) or 1} if vdevs else None
            if want is None and have is None:
                continue
            if want is None or have is None:
                r.diff(f"{w} topology.{cls}", want, have and f"{have['vdevs']} x {have['type']} x {have['width']}")
                continue
            for k in ("vdevs", "type", "width"):
                r.diff(f"{w} topology.{cls}.{k}", want[k], have[k])
    for name in pools:
        if name not in rec.get("pools", {}):
            r.extra(f"storage.yaml pools.{name}")

    disks = rec.get("disks", {}).get("all", {})
    for d in host["disk.query"]:
        for k in ("hddstandby", "advpowermgmt"):
            if k in disks and norm(str(disks[k])) != norm(str(d[k])):
                r.diff(f"storage.yaml disks.all {k} ({d['name']})", disks[k], d[k])

    datasets = by(host["pool.dataset.query"], "id")
    for name, d in rec.get("datasets", {}).items():
        w = f"storage.yaml datasets.{name}"
        if name not in datasets:
            r.missing(w)
            continue
        hd = datasets[name]
        for k in ("compression", "acltype", "aclmode", "recordsize"):
            if k in d:
                r.diff(f"{w} {k}", d[k], prop(hd, k))
        if "atime" in d:
            r.diff(f"{w} atime", d["atime"], prop(hd, "atime") == "ON")
        if "quota" in d:
            q = hd["quota"].get("parsed") if isinstance(hd["quota"], dict) else hd["quota"]
            r.diff(f"{w} quota", d["quota"], d["quota"] if size(d["quota"]) == (q or None) else _bytes(q))
        if "permissions" in d:
            mp = prop(hd, "mountpoint") or f"/mnt/{name}"
            check_permissions(w, d["permissions"], mp, host, ids, r)
    for name in datasets:
        if name not in rec.get("datasets", {}):
            r.extra(f"storage.yaml datasets.{name}")

    tasks = host["pool.snapshottask.query"]
    seen = set()
    for name, t in rec.get("periodic_snapshot_tasks", {}).items():
        w = f"storage.yaml periodic_snapshot_tasks.{name}"
        match = [h for h in tasks if h["dataset"] == t["dataset"] and h["naming_schema"] == t["naming_schema"]]
        if not match:
            r.missing(w)
            continue
        h = match[0]
        seen.add(h["id"])
        r.diff(f"{w} recursive", t.get("recursive", False), h["recursive"])
        r.diff(f"{w} lifetime", t["lifetime"], f"{h['lifetime_value']} {h['lifetime_unit']}")
        r.diff(f"{w} allow_empty", t.get("allow_empty", True), h["allow_empty"])
        r.diff(f"{w} enabled", t.get("enabled", True), h["enabled"])
        r.diff(f"{w} exclude", t.get("exclude", []), h["exclude"])
        r.diff(f"{w} schedule", sched_text(t["schedule"]), sched_text(h["schedule"]))
    for h in tasks:
        if h["id"] not in seen:
            r.extra(f"storage.yaml periodic_snapshot_tasks ({h['dataset']}, {h['naming_schema']})")

    scrubs = by(host["pool.scrub.query"], "pool_name")
    for name, s in rec.get("scrubs", {}).items():
        w = f"storage.yaml scrubs.{name}"
        if name not in scrubs:
            r.missing(w)
            continue
        h = scrubs[name]
        r.diff(f"{w} schedule", sched_text(s["schedule"]), sched_text(h["schedule"]))
        r.diff(f"{w} threshold_days", s["threshold_days"], h["threshold"])
        r.diff(f"{w} enabled", s.get("enabled", True), h["enabled"])
    for name in scrubs:
        if name not in rec.get("scrubs", {}):
            r.extra(f"storage.yaml scrubs.{name}")


def _bytes(n):
    if not n:
        return None
    for unit in ("TiB", "GiB", "MiB", "KiB"):
        if n % SIZES[unit.upper()] == 0:
            return f"{n // SIZES[unit.upper()]} {unit}"
    return str(n)


def sched_text(s):
    s = schedule(s)
    return " ".join(s[k] for k in ("minute", "hour", "dom", "month", "dow"))


PERMS = ("READ", "WRITE", "EXECUTE")


def posix_entry(e):
    tag = {"USER_OBJ": "user:", "GROUP_OBJ": "group:", "OTHER": "other:", "MASK": "mask:",
           "USER": f"user:{e.get('who') or e['id']}", "GROUP": f"group:{e.get('who') or e['id']}"}[e["tag"]]
    return f"{tag}:" + "".join(c if e["perms"].get(p) else "-" for p, c in zip(PERMS, "rwx"))


def nfs4_entry(e):
    perms = e["perms"].get("BASIC") or ",".join(sorted(k for k, v in e["perms"].items() if v))
    flags = e["flags"].get("BASIC") or ",".join(sorted(k for k, v in e["flags"].items() if v))
    return f"{e['tag']}:{e['type']}:{perms}:{flags}"


def check_permissions(where, want, path, host, ids, r):
    """`permissions` is the top directory's mode, owner and group, plus any ACL beyond the mode (and the same
    for each directory listed under `paths`). An ACL not listed must not exist."""
    entries = [("", want)] + [(f"/{ids.login(sub)}", p) for sub, p in want.get("paths", {}).items()]
    for sub, p in entries:
        w = f"{where} permissions{' ' + sub[1:] if sub else ''}"
        st, acl = host["stat"].get(path + sub), host["acl"].get(path + sub)
        if st is None:
            r.missing(f"{w} (path {path + sub})")
            continue
        r.diff(f"{w} mode", f"{int(str(p['mode']), 8):04o}", f"{st['mode'] & 0o7777:04o}")
        r.diff(f"{w} owner", ids.login(p["owner"]), st.get("user") or st["uid"])
        r.diff(f"{w} group", ids.login(p["group"]), st.get("group") or st["gid"])
        if acl is None:
            continue
        if acl["acltype"] == "NFS4":
            r.diff(f"{w} nfs4_acl", p.get("nfs4_acl", []), [nfs4_entry(e) for e in acl["acl"]])
            continue
        base = {"USER_OBJ", "GROUP_OBJ", "OTHER"}
        access = [posix_entry(e) for e in acl["acl"] if not e["default"] and e["tag"] not in base]
        default = [posix_entry(e) for e in acl["acl"] if e["default"]]
        r.diff(f"{w} acl", [ids.login(x) for x in p.get("acl", [])], access)
        r.diff(f"{w} default_acl", [ids.login(x) for x in p.get("default_acl", [])], default)


# ---- sharing -------------------------------------------------------------------------------------------------

def check_sharing(rec, host, ids, r):
    smb = rec.get("smb", {})
    hs = host["smb.config"]
    for k, v in smb.items():
        if k == "purpose":
            continue
        if k == "bindip":
            r.diff("sharing.yaml smb bindip", [ids.address(a) for a in v], hs["bindip"])
        else:
            r.diff(f"sharing.yaml smb {k}", v, hs.get(k))
    nfs = rec.get("nfs", {})
    hn = host["nfs.config"]
    for k, v in nfs.items():
        if k == "purpose":
            continue
        if k == "bindip":
            r.diff("sharing.yaml nfs bindip", [ids.address(a) for a in v], hn["bindip"])
        else:
            r.diff(f"sharing.yaml nfs {k}", v, hn.get(k))

    shares = by(host["sharing.smb.query"], "name")
    for name, s in rec.get("smb_shares", {}).items():
        w = f"sharing.yaml smb_shares.{name}"
        if name not in shares:
            r.missing(w)
            continue
        h = shares[name]
        r.diff(f"{w} path", s["path"], h["path"])
        r.diff(f"{w} share_purpose", s.get("share_purpose", "DEFAULT_SHARE"), h["purpose"])
        r.diff(f"{w} enabled", s.get("enabled", True), h["enabled"])
        r.diff(f"{w} browsable", s.get("browsable", True), h["browsable"])
        r.diff(f"{w} readonly", s.get("readonly", False), h["readonly"])
        r.diff(f"{w} hostsallow", [ids.address(a) for a in s.get("hostsallow", [])], h["options"].get("hostsallow", []))
        audit = s.get("audit")
        r.diff(f"{w} audit.enable", bool(audit), h["audit"]["enable"])
        if audit:
            r.diff(f"{w} audit.watch_list", audit.get("watch_list", []), h["audit"]["watch_list"])
        if "share_acl" in s:
            r.skip(f"{w} share_acl", "hops cannot call sharing.smb.getacl")
    for name in shares:
        if name not in rec.get("smb_shares", {}):
            r.extra(f"sharing.yaml smb_shares.{name}")

    exports = by(host["sharing.nfs.query"], "path")
    seen = set()
    for name, e in rec.get("nfs_exports", {}).items():
        w = f"sharing.yaml nfs_exports.{name}"
        if e["path"] not in exports:
            r.missing(w)
            continue
        h = exports[e["path"]]
        seen.add(e["path"])
        r.diff(f"{w} hosts", [ids.address(a) for a in e.get("hosts", [])], h["hosts"])
        r.diff(f"{w} networks", [ids.cidr(a) for a in e.get("networks", [])], h["networks"])
        r.diff(f"{w} ro", e.get("ro", False), h["ro"])
        r.diff(f"{w} enabled", e.get("enabled", True), h["enabled"])
        mapall = f"{h['mapall_user']}:{h['mapall_group']}" if h["mapall_user"] else None
        maproot = f"{h['maproot_user']}:{h['maproot_group']}" if h["maproot_user"] else None
        r.diff(f"{w} mapall", e.get("mapall"), mapall)
        r.diff(f"{w} maproot", e.get("maproot"), maproot)
    for path in exports:
        if path not in seen:
            r.extra(f"sharing.yaml nfs_exports ({path})")


# ---- system --------------------------------------------------------------------------------------------------

def check_system(rec, host, ids, r):
    gen = host["system.general.config"]
    for k, v in rec.get("general", {}).items():
        w = f"system.yaml general {k}"
        if k == "purpose":
            continue
        if k in ("ui_address", "ui_v6address"):
            r.diff(w, [ids.address(a) for a in v], gen[k])
        elif k == "ui_certificate":
            r.skip(w, "system.general.config masks ui_certificate")
        else:
            r.diff(w, v, gen.get(k))
    for section, method in (("advanced", "system.advanced.config"), ("update", "update.config"),
                            ("docker", "docker.config")):
        h = host[method]
        for k, v in rec.get(section, {}).items():
            if k == "purpose":
                continue
            if k == "address_pools":
                v = [f"{p['base']}/{p['size']}" for p in v]
                r.diff(f"system.yaml {section} {k}", v, [f"{p['base']}/{p['size']}" for p in h[k]])
            else:
                r.diff(f"system.yaml {section} {k}", v, h.get(k))
    if "ntp_servers" in rec:
        r.diff("system.yaml ntp_servers servers", rec["ntp_servers"]["servers"],
               [s["address"] for s in host["system.ntpserver.query"]])
    ann = host["network.configuration.config"]["service_announcement"]
    for k, v in rec.get("network_announcement", {}).items():
        if k != "purpose":
            r.diff(f"system.yaml network_announcement {k}", v, ann.get(k))

    scripts = list(host["initshutdownscript.query"])
    for name, s in rec.get("init_shutdown_scripts", {}).items():
        w = f"system.yaml init_shutdown_scripts.{name}"
        match = [h for h in scripts if h["when"] == s["when"] and h["type"] == s["type"]]
        if not match:
            r.missing(w)
            continue
        h = match[0]
        scripts.remove(h)
        r.diff(f"{w} timeout", s["timeout"], h["timeout"])
        r.diff(f"{w} enabled", s.get("enabled", True), h["enabled"])
        r.skip(f"{w} command", "hops redacts init/shutdown script commands")
    for h in scripts:
        r.extra(f"system.yaml init_shutdown_scripts ({h['when']} {h['type']})")

    tunables = by(host["tunable.query"], "var")
    for name, t in rec.get("tunables", {}).items():
        w = f"system.yaml tunables.{name}"
        if name not in tunables:
            r.missing(w)
            continue
        r.diff(f"{w} type", t["type"], tunables[name]["type"])
        r.diff(f"{w} value", str(t["value"]), str(tunables[name]["value"]))
        r.diff(f"{w} enabled", t.get("enabled", True), tunables[name]["enabled"])
    for name in tunables:
        if name not in rec.get("tunables", {}):
            r.extra(f"system.yaml tunables.{name}")

    jobs = by(host["cronjob.query"], "description")
    for name, j in rec.get("cron_jobs", {}).items():
        w = f"system.yaml cron_jobs.{name}"
        if j["description"] not in jobs:
            r.missing(w)
            continue
        h = jobs[j["description"]]
        r.diff(f"{w} user", j["user"], h["user"])
        r.diff(f"{w} schedule", sched_text(j["schedule"]), sched_text(h["schedule"]))
        r.diff(f"{w} enabled", j.get("enabled", True), h["enabled"])
        r.skip(f"{w} command", "hops redacts cron job commands")
    wanted = {j["description"] for j in rec.get("cron_jobs", {}).values()}
    for d in jobs:
        if d not in wanted:
            r.extra(f"system.yaml cron_jobs ({d})")

    certs = {c["name"] for c in host["certificate.query"]}
    wanted = {ids.name(role) for role in rec.get("certificates", {})}
    for role in rec.get("certificates", {}):
        if ids.name(role) not in certs:
            r.missing(f"system.yaml certificates.{role}")
    for c in certs - wanted:
        r.extra(f"system.yaml certificates ({c})")


# ---- services ------------------------------------------------------------------------------------------------

def check_services(rec, host, ids, r):
    listed = rec.get("services", {})
    for s in host["service.query"]:
        want = listed.get(s["service"], {}).get("enable", False)
        r.diff(f"services.yaml services.{s['service']} enable", want, s["enable"])
    for name in listed:
        if name not in {s["service"] for s in host["service.query"]}:
            r.missing(f"services.yaml services.{name}")
    ssh = host["ssh.config"]
    for k, v in rec.get("ssh", {}).items():
        if k == "purpose":
            continue
        if isinstance(ssh.get(k), str) and ssh[k].startswith("[REDACTED"):
            r.skip(f"services.yaml ssh {k}", "hops redacts it")
        else:
            r.diff(f"services.yaml ssh {k}", v, ssh.get(k))
    if "two_factor" in rec:
        r.diff("services.yaml two_factor enabled", rec["two_factor"]["enabled"],
               host["auth.twofactor.config"]["enabled"])
    if "audit" in rec:
        r.diff("services.yaml audit retention", rec["audit"]["retention"], host["audit.config"]["retention"])


# ---- identities ----------------------------------------------------------------------------------------------

def check_identities(rec, host, ids, r, tsv):
    tsv_users, tsv_groups = tsv
    gid_name = {g["id"]: g["group"] for g in host["group.query"]}
    uid_name = {u["id"]: u["username"] for u in host["user.query"]}
    users = {u["username"]: u for u in host["user.query"] if not u["builtin"]}
    groups = {g["group"]: g for g in host["group.query"] if not g["builtin"]}

    rec_users = {ids.login(k): (k, v) for k, v in rec.get("users", {}).items()}
    for login, (key, u) in rec_users.items():
        w = f"identities.yaml users.{key}"
        if login not in users:
            r.missing(w)
            continue
        h = users[login]
        r.diff(f"{w} uid", u["uid"], h["uid"])
        r.diff(f"{w} group", ids.login(u["group"]), h["group"]["bsdgrp_group"])
        r.diff(f"{w} groups", [ids.login(g) for g in u.get("groups", [])], [gid_name.get(i, i) for i in h["groups"]])
        r.diff(f"{w} home", ids.login(u["home"]), h["home"])
        r.diff(f"{w} shell", u["shell"], h["shell"])
        r.diff(f"{w} smb", u.get("smb", False), h["smb"])
        r.diff(f"{w} password_disabled", u.get("password_disabled", False), h["password_disabled"])
        r.diff(f"{w} ssh_key", u.get("ssh_key", False), bool(h["sshpubkey"]))
        r.diff(f"{w} locked", u.get("locked", False), h["locked"])
        if "sudo_nopasswd" in u:
            r.skip(f"{w} sudo_nopasswd", "hops redacts sudo commands")
    for login, h in users.items():
        if login not in rec_users and login not in tsv_users:
            r.extra(f"identities.yaml users.{login}")
    for name, uid in tsv_users.items():
        if name not in users:
            r.missing(f"identities.tsv user {name}")
        elif users[name]["uid"] != uid:
            r.diff(f"identities.tsv user {name} uid", uid, users[name]["uid"])

    rec_groups = {ids.login(k): (k, v) for k, v in rec.get("groups", {}).items()}
    for name, (key, g) in rec_groups.items():
        w = f"identities.yaml groups.{key}"
        if name not in groups:
            r.missing(w)
            continue
        h = groups[name]
        r.diff(f"{w} gid", g["gid"], h["gid"])
        if "members" in g:
            r.diff(f"{w} members", [ids.login(m) for m in g["members"]], [uid_name.get(i, i) for i in h["users"]])
    for name in groups:
        if name not in rec_groups and name not in tsv_groups:
            r.extra(f"identities.yaml groups.{name}")
    for name, gid in tsv_groups.items():
        if name not in groups:
            r.missing(f"identities.tsv group {name}")
        elif groups[name]["gid"] != gid:
            r.diff(f"identities.tsv group {name} gid", gid, groups[name]["gid"])

    keys = by(host["api_key.query"], "name")
    for name, k in rec.get("api_keys", {}).items():
        w = f"identities.yaml api_keys.{name}"
        if name not in keys:
            r.missing(w)
            continue
        r.diff(f"{w} user", k["user"], keys[name]["username"])
        r.diff(f"{w} expires", k.get("expires", "never"), "never" if keys[name]["expires_at"] is None else "set")
        r.diff(f"{w} revoked", False, keys[name]["revoked"])
    for name in keys:
        if name not in rec.get("api_keys", {}):
            r.extra(f"identities.yaml api_keys.{name}")


# ---- network -------------------------------------------------------------------------------------------------

def check_network(rec, host, ids, r):
    ifaces = by(host["interface.query"], "name")
    listed = rec.get("interfaces", {})
    for name, i in listed.items():
        w = f"network.yaml interfaces.{name}"
        if name not in ifaces:
            r.missing(w)
            continue
        h = ifaces[name]
        for k in ("type", "description", "mtu", "ipv4_dhcp", "ipv6_auto"):
            if k in i:
                r.diff(f"{w} {k}", i[k], h.get(k))
        r.diff(f"{w} aliases", [ids.cidr(a) for a in i.get("aliases", [])],
               [f"{a['address']}/{a['netmask']}" for a in h["aliases"]])
    for name, h in ifaces.items():
        if name not in listed and (h["aliases"] or h["ipv4_dhcp"] or h["ipv6_auto"]):
            r.extra(f"network.yaml interfaces.{name} (configured)")

    g = rec.get("global", {})
    net = host["network.configuration.config"]
    if "ipv4_gateway" in g:
        r.diff("network.yaml global ipv4_gateway", ids.address(g["ipv4_gateway"]), net["ipv4gateway"])
    if "ipv6_gateway" in g:
        r.diff("network.yaml global ipv6_gateway", ids.address(g["ipv6_gateway"]), net["ipv6gateway"])
    if "nameservers" in g:
        have = [net[f"nameserver{n}"] for n in (1, 2, 3) if net.get(f"nameserver{n}")]
        r.diff("network.yaml global nameservers", [ids.address(a) for a in g["nameservers"]], have)
    if "domain" in g:
        r.diff("network.yaml global domain", ids.name(g["domain"]), net["domain"])
    if "static_routes" in g:
        r.diff("network.yaml global static_routes", g["static_routes"],
               [f"{s['destination']} via {s['gateway']}" for s in host["staticroute.query"]])


# ---- Host reads ----------------------------------------------------------------------------------------------

METHODS = (
    "pool.query", "disk.query", "pool.dataset.query", "pool.snapshottask.query", "pool.scrub.query",
    "smb.config", "nfs.config", "sharing.smb.query", "sharing.nfs.query",
    "system.general.config", "system.advanced.config", "update.config", "docker.config",
    "system.ntpserver.query", "network.configuration.config", "initshutdownscript.query", "tunable.query",
    "cronjob.query", "certificate.query", "service.query", "ssh.config", "auth.twofactor.config", "audit.config",
    "user.query", "group.query", "api_key.query", "interface.query", "staticroute.query",
)


def hops_call(method, *params, attempts=8, wait=15):
    # Each call is a fresh API-key login, and TrueNAS rate-limits logins (EBUSY), so back off and retry.
    cmd = ["hops", "truenas", "call", "--json", method, *[json.dumps(p) for p in params]]
    for attempt in range(attempts):
        out = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
        if not out.returncode:
            return json.loads(out.stdout)
        if "Rate Limit" not in out.stderr or attempt == attempts - 1:
            raise RuntimeError(f"hops truenas call {method} failed: {out.stderr.strip()[:300]}")
        time.sleep(wait)


def permission_paths(record, ids):
    paths = []
    for name, d in record["storage"].get("datasets", {}).items():
        if "permissions" in d:
            base = f"/mnt/{name}"
            paths += [base] + [f"{base}/{ids.login(sub)}" for sub in d["permissions"].get("paths", {})]
    return paths


def read_host(record, ids, call=hops_call, workers=4):
    paths = permission_paths(record, ids)
    with ThreadPoolExecutor(workers) as pool:
        futures = {m: pool.submit(call, m) for m in METHODS}
        stats = {p: pool.submit(call, "filesystem.stat", p) for p in paths}
        acls = {p: pool.submit(call, "filesystem.getacl", p) for p in paths}
        host = {m: f.result() for m, f in futures.items()}

        def ok(f):
            try:
                return f.result()
            except RuntimeError as e:
                if "not found" in str(e).lower() or "no such file" in str(e).lower():
                    return None
                raise
        host["stat"] = {p: ok(f) for p, f in stats.items()}
        host["acl"] = {p: ok(f) for p, f in acls.items()}
    return host


def compare(record, host, ids, tsv):
    r = Report()
    check_storage(record["storage"], host, ids, r)
    check_sharing(record["sharing"], host, ids, r)
    check_system(record["system"], host, ids, r)
    check_services(record["services"], host, ids, r)
    check_identities(record["identities"], host, ids, r, tsv)
    check_network(record["network"], host, ids, r)
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--unchecked", action="store_true", help="also list settings that could not be compared")
    args = ap.parse_args(argv)
    try:
        record = load_record()
        ids = Identifiers.from_sops()
        tsv = load_identities_tsv()
        host = read_host(record, ids)
        r = compare(record, host, ids, tsv)
    except (RecordError, RuntimeError) as e:
        print(f"truenas-drift: {e}", file=sys.stderr)
        return 2
    for f in r.drift:
        print(ids.redact(f"{f.where}: {f.message}"))
    if args.unchecked:
        for f in r.unchecked:
            print(ids.redact(f"unchecked {f.where}: {f.message}"))
    return 1 if r.drift else 0


if __name__ == "__main__":
    sys.exit(main())
