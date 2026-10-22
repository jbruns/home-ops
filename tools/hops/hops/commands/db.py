"""hops db <datastore>: the Datastore's own client in its container, authenticated from inside it.

The client never sees a credential. A shell in the container reads it (MariaDB's root password
file, Valkey's requirepass, Postgres's environment) and hands it to the Datastore client in an
environment variable (MYSQL_PWD, REDISCLI_AUTH, PGPASSWORD), so it is in no process argument on
the Host either. An -e SQL statement travels on stdin. Valkey's REPL exits 0 at the end of stdin
even after an error, so each -e line runs as its own `valkey-cli -e <args…>`, which exits
non-zero when the command fails.
"""
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from hops import docker, terminal
from hops.errors import UsageError

HELP = "the Datastore's own client (MariaDB, Valkey, Postgres), interactive or one statement with -e"

# Each script runs as `sh -c SCRIPT hops-db <arg> <client args…>`; only Valkey uses <arg> (its config file).
MARIADB = (
    'shift; p=; '
    'if [ -n "${MARIADB_ROOT_PASSWORD_FILE:-}" ]; then p=$(cat "$MARIADB_ROOT_PASSWORD_FILE") || exit 1; '
    'elif [ -n "${MYSQL_ROOT_PASSWORD_FILE:-}" ]; then p=$(cat "$MYSQL_ROOT_PASSWORD_FILE") || exit 1; '
    'else p=${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}; fi; '
    'if [ -z "$p" ]; then echo "hops: no MARIADB_ROOT_PASSWORD(_FILE) in the container" >&2; exit 1; fi; '
    'export MYSQL_PWD="$p"; '
    'c=$(command -v mariadb || command -v mysql) || { echo "hops: no mariadb client" >&2; exit 127; }; '
    'exec "$c" --user=root "$@"'
)
VALKEY = (
    'p=; if [ -n "$1" ] && [ -r "$1" ]; then p=$(sed -n "s/^requirepass //p" "$1"); fi; shift; '
    'p=${p:-${VALKEY_PASSWORD:-${REDIS_PASSWORD:-}}}; '
    'if [ -n "$p" ]; then export REDISCLI_AUTH="$p"; fi; '
    'c=$(command -v valkey-cli || command -v redis-cli) || { echo "hops: no valkey-cli" >&2; exit 127; }; '
    'exec "$c" "$@"'
)
POSTGRES = (
    'shift; u=${POSTGRES_USER:-postgres}; '
    'if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then PGPASSWORD=$(cat "$POSTGRES_PASSWORD_FILE") || exit 1; '
    'else PGPASSWORD=${POSTGRES_PASSWORD:-}; fi; '
    'if [ -n "$PGPASSWORD" ]; then export PGPASSWORD; else unset PGPASSWORD; fi; '
    'exec psql -X -U "$u" -d "${POSTGRES_DB:-$u}" "$@"'
)

# The -e allowlist: a speed bump against slips, not a security boundary (README).
SQL_READ = {"SELECT", "SHOW", "EXPLAIN", "DESCRIBE", "DESC"}
SQL_FIRST_WORD = re.compile(r"\(*\s*([A-Za-z]+)\b")
VALKEY_READ = {
    "PING", "ECHO", "INFO", "DBSIZE", "TIME", "LASTSAVE", "ROLE", "EXISTS", "TYPE", "TTL", "PTTL",
    "EXPIRETIME", "PEXPIRETIME", "KEYS", "SCAN", "RANDOMKEY", "GET", "MGET", "STRLEN", "GETRANGE",
    "GETBIT", "BITCOUNT", "BITPOS", "LCS", "HGET", "HMGET", "HGETALL", "HKEYS", "HVALS", "HLEN",
    "HEXISTS", "HSTRLEN", "HSCAN", "HRANDFIELD", "LRANGE", "LLEN", "LINDEX", "LPOS", "SMEMBERS",
    "SISMEMBER", "SMISMEMBER", "SCARD", "SSCAN", "SRANDMEMBER", "SINTER", "SINTERCARD", "SUNION",
    "SDIFF", "ZRANGE", "ZRANGEBYSCORE", "ZRANGEBYLEX", "ZREVRANGE", "ZREVRANGEBYSCORE",
    "ZREVRANGEBYLEX", "ZRANK", "ZREVRANK", "ZSCORE", "ZMSCORE", "ZCARD", "ZCOUNT", "ZLEXCOUNT",
    "ZSCAN", "ZRANDMEMBER", "XLEN", "XRANGE", "XREVRANGE", "PFCOUNT", "GEOPOS", "GEODIST",
    "GEOHASH", "GEOSEARCH",
}
VALKEY_READ_SUBCOMMANDS = {
    "CLIENT": {"LIST", "INFO", "GETNAME", "ID"},
    "COMMAND": {"COUNT", "DOCS", "INFO", "LIST"},
    "LATENCY": {"LATEST", "HISTORY", "DOCTOR"},
    "MEMORY": {"USAGE", "STATS", "DOCTOR"},
    "OBJECT": {"ENCODING", "FREQ", "IDLETIME", "REFCOUNT"},
    "SLOWLOG": {"GET", "LEN"},
    "XINFO": {"STREAM", "GROUPS", "CONSUMERS"},
}


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "db", help=HELP,
        description=HELP + ". Without -e it is Disruptive: it needs a TTY (ssh -t, docker exec -it). "
        "Credentials are read inside the container and never reach this machine.")
    p.add_argument("datastore", help="a Datastore Service: stack/service, or a unique Service name")
    p.add_argument("-e", dest="statement", metavar="STATEMENT",
                   help="run one statement non-interactively (Valkey: one command per line) and print the result")
    p.add_argument("--write", action="store_true",
                   help="allow an -e statement that does not start with SELECT, SHOW, EXPLAIN or DESCRIBE "
                        "(Valkey: a read command)")
    p.set_defaults(func=run)


def sql_read(statement: str) -> bool:
    """Every ;-separated statement starts with a read keyword, and there is no client command: psql
    and mariadb act on a backslash command (\\g, \\!) anywhere in a line."""
    statements = [s for s in statement.split(";") if s.strip()]
    return "\\" not in statement and bool(statements) and all(
        (m := SQL_FIRST_WORD.match(s.strip())) and m[1].upper() in SQL_READ for s in statements)


def valkey_read(statement: str) -> bool:
    """Every line is a read command."""
    commands = [line for line in statement.splitlines() if line.strip()]
    return bool(commands) and all(_valkey_read(line) for line in commands)


def valkey_commands(statement: str) -> list[list[str]]:
    """Each non-empty line as valkey-cli arguments, quoted as in valkey-cli ('…', "…")."""
    try:
        commands = [shlex.split(line) for line in statement.splitlines() if line.strip()]
    except ValueError as exc:
        raise UsageError(f"cannot parse the -e Valkey command: {exc}") from None
    if not commands:
        raise UsageError("the -e statement has no Valkey command")
    return [["-e", *words] for words in commands]


def _valkey_read(line: str) -> bool:
    try:
        words = [w.upper() for w in shlex.split(line)]
    except ValueError:
        return False
    if words[0] in VALKEY_READ_SUBCOMMANDS:
        return len(words) > 1 and words[1] in VALKEY_READ_SUBCOMMANDS[words[0]]
    return words[0] in VALKEY_READ


@dataclass(frozen=True)
class Engine:
    script: str
    read: Callable[[str], bool]
    reads: str  # what read() allows, for the refusal message
    one_shot_args: tuple[str, ...] = ()
    # Splits an -e statement into one client run each (its arguments); None: the statement goes on stdin.
    commands: Callable[[str], list[list[str]]] | None = None


SQL_READS = "SELECT, SHOW, EXPLAIN or DESCRIBE"
ENGINES = {
    "mariadb": Engine(MARIADB, sql_read, SQL_READS),
    "valkey": Engine(VALKEY, valkey_read, "a read command", commands=valkey_commands),
    "postgres": Engine(POSTGRES, sql_read, SQL_READS, ("-v", "ON_ERROR_STOP=1")),
}


def read_only(engine: str, statement: str) -> bool:
    return ENGINES[engine].read(statement)


def config_file(service) -> str:
    """The config file named in the Service's command (Valkey keeps requirepass there), or ''."""
    command = service.spec.get("command") or []
    words = shlex.split(command) if isinstance(command, str) else [str(w) for w in command]
    return next((w for w in words if w.endswith(".conf")), "")


def run(args, ctx) -> int:
    if args.write and args.statement is None:
        raise UsageError("--write only applies to a statement given with -e")
    service = ctx.declared.resolve_one(args.datastore)
    if service.datastore is None:
        raise UsageError(f"{service.key} is not a Datastore (a MariaDB, Valkey or Postgres image)")
    engine = ENGINES[service.datastore]
    script = ["sh", "-c", engine.script, "hops-db", config_file(service)]
    if args.statement is None:
        terminal.require_tty(f"hops db {service.key} without -e")
        return ctx.runner.attach("exec", "-it", docker.container_ref(ctx.runner, service), *script, tty=True)
    if not args.write and not engine.read(args.statement):
        raise UsageError(f"the -e statement is not {engine.reads}; add --write to run it")
    if engine.commands:
        runs = [(argv, None) for argv in engine.commands(args.statement)]
    else:
        statement = args.statement if args.statement.endswith("\n") else args.statement + "\n"
        runs = [(list(engine.one_shot_args), statement)]
    container = docker.container_ref(ctx.runner, service)
    for argv, statement in runs:
        result = ctx.runner.docker("exec", *(["-i"] if statement else []), container, *script, *argv,
                                   input=statement, check=False)
        ctx.stdout.write(result.stdout)
        ctx.stderr.write(result.stderr)
        if result.returncode:
            return result.returncode
    return 0
