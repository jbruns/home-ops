"""host/create_usersgroups.sh against stubbed `cli` and `getent` commands."""
import os
import subprocess
from pathlib import Path

import pytest

HOST_DIR = Path(__file__).resolve().parents[1]
SCRIPT = HOST_DIR / "create_usersgroups.sh"

# The commands a fresh Host gets from identities.tsv, in order: the identity set the data file must keep
# (docs/research/0552-unix-identities.md). Names are quoted as midcli strings ("media").
SERVICE_USERS = [("sonarr", 4001), ("radarr", 4002), ("sabnzbd", 4003), ("navidrome", 4004), ("plex", 4005),
                 ("emby", 4006), ("qbittor", 4007), ("cwa", 4008), ("shelfmark", 4009), ("audiobookshelf", 4010),
                 ("nextpvr", 4011), ("ownfoil", 4012), ("autobrr", 4013), ("media", 1001)]
ALL_COMMANDS = [
    'account group create gid=1002 name="media" smb=false',
    *[f'account user create username="{name}" full_name="{name}" uid={uid} group_create=false home_create=false '
      f'password_disabled=true home=/var/empty group="media" smb=false shell=/usr/sbin/nologin'
      for name, uid in SERVICE_USERS],
]

CLI_STUB = """#!/bin/sh
[ "$1" = "-c" ] || { echo "stub cli: expected -c" >&2; exit 2; }
printf '%s\\n' "$2" >>"$CLI_LOG"
case "$2" in *"${CLI_FAIL:-<none>}"*) echo "stub cli: failed" >&2; exit 1 ;; esac
"""

# getent <db> <key>: print the line whose name (field 1) or id (field 3) is key, as glibc getent does.
GETENT_STUB = """#!/bin/sh
f="$GETENT_DIR/$1"
[ -f "$f" ] || exit 2
awk -F: -v k="$2" '$1 == k || $3 == k { print; found = 1 } END { exit found ? 0 : 2 }' "$f"
"""


class Host:
    def __init__(self, root: Path):
        self.bin, self.db, self.log = root / "bin", root / "db", root / "cli.log"
        for d in (self.bin, self.db):
            d.mkdir()
        for name, body in (("cli", CLI_STUB), ("getent", GETENT_STUB)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log.write_text("")

    def has_group(self, name: str, gid: int) -> None:
        with (self.db / "group").open("a") as f:
            f.write(f"{name}:x:{gid}:\n")

    def has_user(self, name: str, uid: int) -> None:
        with (self.db / "passwd").open("a") as f:
            f.write(f"{name}:x:{uid}:{uid}::/var/empty:/usr/sbin/nologin\n")

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
        env = {**os.environ, **extra_env, "PATH": f"{self.bin}:{os.environ['PATH']}", "CLI_LOG": str(self.log),
               "GETENT_DIR": str(self.db)}
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)

    @property
    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def test_fresh_host_gets_the_full_identity_set(host):
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert host.calls == ALL_COMMANDS


def test_dry_run_prints_commands_and_changes_nothing(host):
    r = host.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert host.calls == []
    assert r.stdout.splitlines() == [f"cli -c '{c}'" for c in ALL_COMMANDS]


def test_rerun_skips_what_exists(host):
    host.has_group("media", 1002)
    host.has_user("sonarr", 4001)
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert host.calls == [c for c in ALL_COMMANDS if ' name="media" ' not in c and "sonarr" not in c]
    assert "# skip: group media exists (gid 1002)" in r.stdout
    assert "# skip: user sonarr exists (uid 4001)" in r.stdout


def test_second_run_is_a_no_op(host):
    for c in ALL_COMMANDS:
        kind = "group" if c.startswith("account group") else "user"
        fields = {k: v.strip('"') for k, v in (f.split("=", 1) for f in c.split()[3:])}
        if kind == "group":
            host.has_group(fields["name"], int(fields["gid"]))
        else:
            host.has_user(fields["username"], int(fields["uid"]))
    r = host.run()
    assert r.returncode == 0, r.stderr
    assert host.calls == []


def test_id_conflicts_fail_but_the_rest_is_created(host):
    host.has_group("media", 1002)
    host.has_user("plex", 999)          # right name, wrong uid
    host.has_user("someone", 4001)      # sonarr's uid taken by another user
    r = host.run()
    assert r.returncode == 1
    assert "user plex exists with uid 999, expected 4005" in r.stderr
    assert "uid 4001 for user sonarr is taken by someone" in r.stderr
    assert host.calls == [c for c in ALL_COMMANDS
                          if ' name="media" ' not in c and 'username="plex" ' not in c
                          and 'username="sonarr" ' not in c]


def test_a_missing_primary_group_blocks_its_users(host):
    host.has_group("other", 1002)       # media's gid taken by another group
    r = host.run()
    assert r.returncode == 1
    assert "gid 1002 for group media is taken by other" in r.stderr
    assert "user radarr not created: missing group(s) media" in r.stderr
    assert host.calls == []


def test_cli_failure_is_reported_and_the_run_continues(host):
    r = host.run(CLI_FAIL='username="plex" ')
    assert r.returncode == 1
    assert 'ERROR: cli failed: account user create username="plex"' in r.stderr
    assert host.calls == ALL_COMMANDS


def test_file_option_and_supplementary_groups(host, tmp_path):
    host.has_group("media", 1002)
    data = tmp_path / "ids.tsv"
    data.write_text("# comment\n\ngroup  app  5000  false\nuser   app  5000  app  -\nuser   two  5001  app  app,media\n")
    r = host.run("--file", str(data))
    assert r.returncode == 0, r.stderr
    assert host.calls == [
        'account group create gid=5000 name="app" smb=false',
        'account user create username="app" full_name="app" uid=5000 group_create=false home_create=false '
        'password_disabled=true home=/var/empty group="app" smb=false shell=/usr/sbin/nologin',
        'account user create username="two" full_name="two" uid=5001 group_create=false home_create=false '
        'password_disabled=true home=/var/empty group="app" groups="app","media" smb=false shell=/usr/sbin/nologin',
    ]


def test_hyphenated_names_are_quoted_for_midcli(host, tmp_path):
    data = tmp_path / "ids.tsv"
    data.write_text("group  my-group  5000  false\ngroup  x.y-z  5001  false\n"
                    "user   my-app    5000  my-group  my-group,x.y-z\n")
    r = host.run("--file", str(data), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        """cli -c 'account group create gid=5000 name="my-group" smb=false'""",
        """cli -c 'account group create gid=5001 name="x.y-z" smb=false'""",
        """cli -c 'account user create username="my-app" full_name="my-app" uid=5000 group_create=false """
        """home_create=false password_disabled=true home=/var/empty group="my-group" groups="my-group","x.y-z" """
        """smb=false shell=/usr/sbin/nologin'""",
    ]


@pytest.mark.parametrize("line", ["group app 5000", "group app x false", "user app 5000 app", "host app 1 a b",
                                  "group app 5000 maybe", 'group a"b 5001 false', "group a\\b 5001 false",
                                  'user a"b 5001 ok -', "user app 5001 a\\b -", 'user app 5001 ok ok,a"b',
                                  "user app 5001 ok ok,,ok", "group 'a' 5001 false"])
def test_malformed_data_changes_nothing(host, tmp_path, line):
    data = tmp_path / "ids.tsv"
    data.write_text(f"group ok 5000 false\n{line}\n")
    r = host.run("--file", str(data))
    assert r.returncode == 2
    assert "ids.tsv:2" in r.stderr
    assert host.calls == []


def test_unknown_option(host):
    r = host.run("--nope")
    assert r.returncode == 2 and "usage:" in r.stderr
