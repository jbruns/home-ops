"""hops db <datastore>: the Datastore's own client, authenticated from inside its container."""
import json
import shlex
import stat
import subprocess
import textwrap

import pytest
from hops_fakes import write

from hops.commands.db import read_only

PG_LITERAL = "pg-literal-secret"


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr("hops.terminal.stdin_is_tty", lambda: True)


@pytest.fixture
def box(tmp_path):
    """Stands in for a container's filesystem: secret files and fake clients that report what they got."""
    root = tmp_path / "box"
    (root / "bin").mkdir(parents=True)
    for client in ("mariadb", "valkey-cli", "psql"):
        path = root / "bin" / client
        path.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            echo "client: {client} $*"
            echo "auth: ${{MYSQL_PWD-unset}} ${{REDISCLI_AUTH-unset}} ${{PGPASSWORD-unset}}"
            [ -t 0 ] || echo "stdin: $(cat)"
            exit 7
        """))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return root


@pytest.fixture
def stores(repo, box):
    """Extra Datastores: Postgres, and Valkey with its password in a config file."""
    write(repo, {
        "tn/vault/compose.yaml": f"""
            services:
              vault-db:
                image: docker.io/library/postgres:17.6@sha256:4444
                container_name: vault-db
                environment:
                  POSTGRES_USER: vault
                  POSTGRES_PASSWORD: {PG_LITERAL}
              cache:
                image: redis:8
                container_name: vault-cache
                command: ["redis-server", "{box}/valkey.conf"]
        """,
    })
    (box / "valkey.conf").write_text("save \"\"\nrequirepass valkey-secret\n")
    return box


def remote(call, box, env, stdin=""):
    """Run the `sh -c` a db call sends to `docker exec`, here, with the box's fake clients on PATH."""
    args = call.docker[0]
    sh = args[args.index("sh"):]
    done = subprocess.run(sh, input=stdin if call.input is None else call.input, capture_output=True, text=True,
                          env={"PATH": f"{box}/bin:/usr/bin:/bin", **env})
    return done.stdout + done.stderr


def all_argv(fake_host) -> str:
    return " ".join(shlex.join(c.argv) for c in fake_host.calls)


def test_db_refuses_a_service_that_is_not_a_datastore(hops, fake_host, tty):
    outcome = hops("db", "sonarr/sonarr")
    assert outcome.code == 2
    assert "not a Datastore" in outcome.err
    assert fake_host.calls == []


def test_interactive_db_refuses_without_a_tty(hops, fake_host):
    outcome = hops("db", "nextcloud/mariadb")
    assert outcome.code == 2
    assert "TTY" in outcome.err
    assert fake_host.calls == []


def test_interactive_mariadb_logs_in_as_root_with_the_password_file_in_the_container(hops, fake_host, box, tty):
    (box / "root_pw").write_text("maria-secret\n")
    fake_host.respond("exec")
    assert hops("db", "nextcloud/mariadb").code == 0
    call = fake_host.calls[0]
    assert call.docker[0][:3] == ["exec", "-it", "mariadb"]
    assert "-t" in call.argv and not call.capture
    out = remote(call, box, {"MARIADB_ROOT_PASSWORD_FILE": f"{box}/root_pw"})
    assert "client: mariadb --user=root\n" in out
    assert "auth: maria-secret unset unset" in out
    assert "maria-secret" not in all_argv(fake_host)


def test_mariadb_without_a_root_password_in_the_container_fails_there(hops, fake_host, box, tty):
    fake_host.respond("exec")
    hops("db", "nextcloud/mariadb")
    out = remote(fake_host.calls[0], box, {})
    assert "client:" not in out
    assert "MARIADB_ROOT_PASSWORD" in out


def test_one_shot_select_sends_the_statement_on_stdin_and_prints_the_result(hops, fake_host, box):
    fake_host.respond("exec", stdout="1\n1\n", stderr="a warning\n", returncode=0)
    outcome = hops("db", "nextcloud/mariadb", "-e", "SELECT 1")
    assert outcome.code == 0
    assert outcome.out == "1\n1\n"
    assert "a warning" in outcome.err
    call = fake_host.calls[0]
    assert call.docker[0][:3] == ["exec", "-i", "mariadb"]
    assert call.input == "SELECT 1\n"
    assert call.capture and "-T" in call.argv
    out = remote(call, box, {"MARIADB_ROOT_PASSWORD": "env-secret"})
    assert "auth: env-secret" in out
    assert "stdin: SELECT 1" in out


def test_one_shot_returns_the_client_exit_code(hops, fake_host):
    fake_host.respond("exec", stderr="ERROR 1146: no such table\n", returncode=1)
    outcome = hops("db", "nextcloud/mariadb", "-e", "SELECT * FROM nope")
    assert outcome.code == 1
    assert "ERROR 1146" in outcome.err


def test_one_shot_write_needs_write_flag(hops, fake_host):
    outcome = hops("db", "nextcloud/mariadb", "-e", "DELETE FROM oc_users")
    assert outcome.code == 2
    assert "--write" in outcome.err
    assert fake_host.calls == []


def test_one_shot_write_runs_with_write_flag(hops, fake_host):
    fake_host.respond("exec")
    assert hops("db", "nextcloud/mariadb", "-e", "DELETE FROM oc_users", "--write").code == 0
    assert fake_host.calls[0].input == "DELETE FROM oc_users\n"


def test_write_flag_needs_a_statement(hops, fake_host):
    outcome = hops("db", "nextcloud/mariadb", "--write")
    assert outcome.code == 2
    assert fake_host.calls == []


def test_valkey_one_shot_runs_each_line_as_valkey_cli_e_with_requirepass_from_its_config(hops, fake_host, stores):
    fake_host.respond("exec")
    assert hops("db", "vault/cache", "-e", "INFO server\nGET 'a key'\n").code == 0
    assert [c.docker[0][:2] for c in fake_host.calls] == [["exec", "vault-cache"]] * 2
    assert [c.input for c in fake_host.calls] == [None, None]
    first, second = (remote(c, stores, {}) for c in fake_host.calls)
    assert "client: valkey-cli -e INFO server\n" in first
    assert "auth: unset valkey-secret unset" in first
    assert "client: valkey-cli -e GET a key\n" in second
    assert fake_host.calls[1].docker[0][-2:] == ["GET", "a key"]
    assert "valkey-secret" not in all_argv(fake_host)


def test_valkey_one_shot_fails_on_a_command_error_and_stops(repo, host_config, stores, capsys):
    """End to end: the in-container script runs here, with a valkey-cli that, like the real one
    with -e, exits non-zero when a command errors."""
    from hops import cli
    from hops.runner import Result, Runner
    from hops_fakes import Call

    (stores / "bin" / "valkey-cli").write_text(textwrap.dedent("""\
        #!/bin/sh
        [ "$1" = -e ] && shift
        echo "ran: $*" >> "$(dirname "$0")/../ran"
        if [ "$1" = GET ] && [ $# -lt 2 ]; then echo "(error) ERR wrong number of arguments for 'get'"; exit 1; fi
        echo "$REDISCLI_AUTH" | grep -q valkey-secret && echo PONG
    """))

    def container(argv, *, input=None, capture=True):
        args = Call(list(argv), input, capture).docker[0]
        done = subprocess.run(args[args.index("sh"):], input=input or "", capture_output=True, text=True,
                              env={"PATH": f"{stores}/bin:/usr/bin:/bin"})
        return Result(list(argv), done.returncode, done.stdout, done.stderr)

    def hops(*argv):
        ctx = cli.Context(repo_root=repo, runner=Runner(host_config, process=container))
        return cli.main(list(argv), ctx=ctx), capsys.readouterr()

    code, out = hops("db", "vault/cache", "-e", "PING")
    assert code == 0 and out.out == "PONG\n"
    code, out = hops("db", "vault/cache", "-e", "PING\nGET\nPING")
    assert code == 1
    assert "wrong number of arguments" in out.out
    assert (stores / "ran").read_text() == "ran: PING\nran: PING\nran: GET\n"


def test_valkey_one_shot_with_unbalanced_quotes_is_a_usage_error(hops, fake_host):
    outcome = hops("db", "nextcloud/valkey", "-e", "GET 'a", "--write")
    assert outcome.code == 2
    assert fake_host.calls == []


def test_valkey_without_a_config_uses_the_password_in_its_environment(hops, fake_host, box, tty):
    fake_host.respond("exec")
    hops("db", "nextcloud/valkey")
    assert fake_host.calls[0].docker[0][:3] == ["exec", "-it", "valkey"]
    assert "auth: unset env-pass unset" in remote(fake_host.calls[0], box, {"REDIS_PASSWORD": "env-pass"})


def test_postgres_logs_in_with_the_user_and_password_from_its_environment(hops, fake_host, stores):
    (stores / "pg_pw").write_text("pg-file-secret\n")
    fake_host.respond("exec")
    assert hops("db", "vault/vault-db", "-e", "SELECT 1").code == 0
    call = fake_host.calls[0]
    assert call.docker[0][:3] == ["exec", "-i", "vault-db"]
    out = remote(call, stores, {"POSTGRES_USER": "vault", "POSTGRES_PASSWORD_FILE": f"{stores}/pg_pw"})
    assert "client: psql -X -U vault -d vault -v ON_ERROR_STOP=1\n" in out
    assert "auth: unset unset pg-file-secret" in out
    assert PG_LITERAL not in all_argv(fake_host)


def test_postgres_defaults_to_the_postgres_user(hops, fake_host, stores, tty):
    fake_host.respond("exec")
    hops("db", "vault/vault-db")
    out = remote(fake_host.calls[0], stores, {"POSTGRES_PASSWORD": "pw"})
    assert "client: psql -X -U postgres -d postgres\n" in out
    assert "auth: unset unset pw" in out


@pytest.mark.parametrize("engine, statement", [
    ("mariadb", "SELECT 1"),
    ("mariadb", "  select * from oc_users;"),
    ("mariadb", "SHOW PROCESSLIST; SHOW DATABASES"),
    ("mariadb", "EXPLAIN SELECT 1"),
    ("mariadb", "describe oc_users"),
    ("mariadb", "(SELECT 1)"),
    ("postgres", "SELECT now()"),
    ("valkey", "PING"),
    ("valkey", "info server"),
    ("valkey", "GET foo\nTTL foo\n"),
    ("valkey", "CLIENT LIST"),
    ("valkey", "SCAN 0 MATCH 'a b*'"),
])
def test_read_statements(engine, statement):
    assert read_only(engine, statement)


@pytest.mark.parametrize("engine, statement", [
    ("mariadb", "DELETE FROM oc_users"),
    ("mariadb", "SELECT 1; DROP TABLE oc_users"),
    ("mariadb", "/* hi */ SELECT 1"),
    ("mariadb", "\\! sh"),
    ("mariadb", "SELECT 1 \\! touch x"),
    ("postgres", "SELECT 1 \\g\nDELETE FROM t"),
    ("mariadb", "SELECTED"),
    ("mariadb", ""),
    ("postgres", "UPDATE t SET a = 1"),
    ("postgres", "\\copy t to x"),
    ("valkey", "FLUSHALL"),
    ("valkey", "GET foo\nDEL foo"),
    ("valkey", "CONFIG GET requirepass"),
    ("valkey", "CLIENT KILL ID 1"),
    ("valkey", "CLIENT"),
    ("valkey", ""),
])
def test_write_statements(engine, statement):
    assert not read_only(engine, statement)


def test_client_side_argv_never_carries_credentials(hops, fake_host, stores, tty):
    fake_host.respond("exec")
    for target in ("nextcloud/mariadb", "nextcloud/valkey", "vault/vault-db", "vault/cache"):
        hops("db", target)
        hops("db", target, "-e", "PING" if "valkey" in target or "cache" in target else "SELECT 1")
    assert len(fake_host.calls) == 8
    assert PG_LITERAL not in all_argv(fake_host) and "valkey-secret" not in all_argv(fake_host)
    for call in fake_host.calls:
        args = call.docker[0]
        assert not [a for a in args if a == "-a" or a.startswith(("-p", "--password", "--pass"))], args


NEXTCLOUD_USERS = "root@localhost,exporter@%,backupuser@%,nextclouduser@%"
SYSTEM_ROWS = "mariadb.sys\tlocalhost\nhealthcheck\t127.0.0.1\nhealthcheck\t::1\nhealthcheck\tlocalhost\n"


def mariadb_with(repo, users_label: str | None) -> None:
    """nextcloud/mariadb (list-form labels) carrying users_label, and vault/db (mapping form) with its own."""
    label = "" if users_label is None else f', "homeops.db.users={users_label}"'
    write(repo, {
        "tn/nextcloud/mariadb/service.yaml": f"""
            services:
              mariadb:
                image: mariadb:11.8.9@sha256:bbbb
                container_name: mariadb
                labels: [tier=critical, startup-order=1{label}]
        """,
        "tn/vault/compose.yaml": """
            services:
              db:
                image: mariadb:11.8.9@sha256:bbbb
                container_name: vault-db
                labels:
                  tier: critical
                  homeops.db.users: "root@localhost, vaultwardenuser@%"
        """,
    })


def users(*rows: str) -> str:
    """mysql.user as `mariadb --batch --skip-column-names` prints it, with the image's system accounts."""
    return SYSTEM_ROWS + "".join("\t".join(r.rpartition("@")[::2]) + "\n" for r in rows)


@pytest.fixture
def labelled(repo):
    mariadb_with(repo, NEXTCLOUD_USERS)
    return repo


def test_users_clean_ignores_the_image_system_accounts(hops, fake_host, labelled, box):
    fake_host.respond("exec", stdout=users("root@localhost", "exporter@%", "backupuser@%", "nextclouduser@%"))
    outcome = hops("db", "nextcloud/mariadb", "--users", "--json")
    assert outcome.code == 0, outcome.err
    report = json.loads(outcome.out)
    assert report["drift"] is False
    [store] = report["datastores"]
    assert store["stack"] == "nextcloud" and store["container"] == "mariadb"
    assert store["declared"] == NEXTCLOUD_USERS.split(",")
    assert "healthcheck@::1" in store["live"]
    assert store["unexpected"] == [] and store["missing"] == []
    call = fake_host.calls[0]
    assert call.docker[0][:3] == ["exec", "-i", "mariadb"]
    assert call.input == "SELECT User, Host FROM mysql.user\n"
    out = remote(call, box, {"MARIADB_ROOT_PASSWORD": "env-secret"})
    assert "client: mariadb --user=root --batch --skip-column-names\n" in out
    assert "auth: env-secret" in out


def test_users_reports_unexpected_and_missing_and_exits_1(hops, fake_host, labelled):
    fake_host.respond("exec", stdout=users("root@localhost", "root@%", "exporter@%", "nextclouduser@%",
                                           "vaultwardenuser@%"))
    outcome = hops("db", "nextcloud/mariadb", "--users", "--json")
    assert outcome.code == 1
    store = json.loads(outcome.out)["datastores"][0]
    assert store["unexpected"] == ["root@%", "vaultwardenuser@%"]
    assert store["missing"] == ["backupuser@%"]
    outcome = hops("db", "nextcloud/mariadb", "--users")
    assert outcome.code == 1
    assert "root@%, vaultwardenuser@%" in outcome.out and "backupuser@%" in outcome.out


def test_users_without_a_datastore_checks_every_labelled_one(hops, fake_host, labelled):
    fake_host.respond("exec", stdout=users("root@localhost", "exporter@%", "backupuser@%", "nextclouduser@%"))
    fake_host.respond("exec", "-i", "vault-db", stdout=users("root@localhost", "vaultwardenuser@%"))
    outcome = hops("db", "--users", "--json")
    assert outcome.code == 0, outcome.err
    assert [(d["stack"], d["service"]) for d in json.loads(outcome.out)["datastores"]] == [
        ("nextcloud", "mariadb"), ("vault", "db")]
    assert [c.docker[0][2] for c in fake_host.calls] == ["mariadb", "vault-db"]


def test_users_query_failure_is_a_host_error(hops, fake_host, labelled):
    fake_host.respond("exec", stderr="ERROR 2002: can't connect\n", returncode=1)
    outcome = hops("db", "nextcloud/mariadb", "--users")
    assert outcome.code == 1
    assert "ERROR 2002" in outcome.err


@pytest.mark.parametrize("argv, message", [
    (("db", "nextcloud/mariadb", "--users", "-e", "SELECT 1"), "-e or --write"),
    (("db", "nextcloud/mariadb", "--users", "--write"), "-e or --write"),
    (("db",), "name a Datastore"),
    (("db", "-e", "SELECT 1"), "name a Datastore"),
    (("db", "nextcloud/mariadb", "--json"), "--json only applies to --users"),
    (("db", "nextcloud/valkey", "--users"), "Postgres and Valkey are out of scope"),
    (("db", "sonarr/sonarr", "--users"), "not a Datastore"),
])
def test_users_usage_errors(hops, fake_host, labelled, argv, message):
    outcome = hops(*argv)
    assert outcome.code == 2
    assert message in outcome.err
    assert fake_host.calls == []


def test_users_needs_the_label_on_a_named_datastore(hops, fake_host, repo):
    mariadb_with(repo, None)
    outcome = hops("db", "nextcloud/mariadb", "--users")
    assert outcome.code == 2
    assert "no homeops.db.users label" in outcome.err
    assert fake_host.calls == []


def test_users_without_a_datastore_needs_a_labelled_one(hops, fake_host):
    outcome = hops("db", "--users")
    assert outcome.code == 2
    assert "no Datastore carries" in outcome.err


@pytest.mark.parametrize("label, message", [
    ("root@localhost,,exporter@%", "''"),
    ("root@localhost,exporter@%,", "''"),
    ("root", "'root'"),
    ("@localhost", "'@localhost'"),
    ("root@", "'root@'"),
    ("root @localhost", "'root @localhost'"),
    ("root@localhost,root@localhost", "twice"),
])
def test_users_rejects_a_malformed_label_before_the_host(hops, fake_host, repo, label, message):
    mariadb_with(repo, label)
    outcome = hops("db", "--users")
    assert outcome.code == 2
    assert message in outcome.err
    assert fake_host.calls == []


def test_users_label_host_splits_on_the_last_at(hops, fake_host, repo):
    mariadb_with(repo, "a@b@%")
    fake_host.respond("exec", stdout=SYSTEM_ROWS + "a@b\t%\n")
    fake_host.respond("exec", "-i", "vault-db", stdout=users("root@localhost", "vaultwardenuser@%"))
    assert hops("db", "--users").code == 0
