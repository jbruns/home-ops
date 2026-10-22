"""hops db <datastore>: the Datastore's own client in its container, authenticated from inside it.

The client never sees a credential. A shell in the container reads it (MariaDB's root password
file, Valkey's requirepass, Postgres's environment) and hands it to the Datastore client in an
environment variable (MYSQL_PWD, REDISCLI_AUTH, PGPASSWORD), so it is in no process argument on
the Host either. The -e statement travels on stdin.
"""
import re
import shlex

from hops import docker, terminal
from hops.errors import UsageError

HELP = "the Datastore's own client (MariaDB, Valkey, Postgres), interactive or one statement with -e"

# Each script runs as `sh -c SCRIPT hops-db <arg> <client args…>`; only Valkey uses <arg> (its config file).
SCRIPTS = {
    "mariadb": (
        'shift; p=; '
        'if [ -n "${MARIADB_ROOT_PASSWORD_FILE:-}" ]; then p=$(cat "$MARIADB_ROOT_PASSWORD_FILE") || exit 1; '
        'elif [ -n "${MYSQL_ROOT_PASSWORD_FILE:-}" ]; then p=$(cat "$MYSQL_ROOT_PASSWORD_FILE") || exit 1; '
        'else p=${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}; fi; '
        'if [ -z "$p" ]; then echo "hops: no MARIADB_ROOT_PASSWORD(_FILE) in the container" >&2; exit 1; fi; '
        'export MYSQL_PWD="$p"; '
        'c=$(command -v mariadb || command -v mysql) || { echo "hops: no mariadb client" >&2; exit 127; }; '
        'exec "$c" --user=root "$@"'
    ),
    "valkey": (
        'p=; if [ -n "$1" ] && [ -r "$1" ]; then p=$(sed -n "s/^requirepass //p" "$1"); fi; shift; '
        'p=${p:-${VALKEY_PASSWORD:-${REDIS_PASSWORD:-}}}; '
        'if [ -n "$p" ]; then export REDISCLI_AUTH="$p"; fi; '
        'c=$(command -v valkey-cli || command -v redis-cli) || { echo "hops: no valkey-cli" >&2; exit 127; }; '
        'exec "$c" "$@"'
    ),
    "postgres": (
        'shift; u=${POSTGRES_USER:-postgres}; '
        'if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then PGPASSWORD=$(cat "$POSTGRES_PASSWORD_FILE") || exit 1; '
        'else PGPASSWORD=${POSTGRES_PASSWORD:-}; fi; '
        'if [ -n "$PGPASSWORD" ]; then export PGPASSWORD; else unset PGPASSWORD; fi; '
        'exec psql -X -U "$u" -d "${POSTGRES_DB:-$u}" "$@"'
    ),
}
ONE_SHOT_ARGS = {"mariadb": [], "valkey": [], "postgres": ["-v", "ON_ERROR_STOP=1"]}

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


def read_only(engine: str, statement: str) -> bool:
    """Whether every statement (SQL: split on ;) or command (Valkey: one per line) is a read."""
    if engine == "valkey":
        commands = [line for line in statement.splitlines() if line.strip()]
        return bool(commands) and all(_valkey_read(line) for line in commands)
    statements = [s for s in statement.split(";") if s.strip()]
    return bool(statements) and all(
        (m := SQL_FIRST_WORD.match(s.strip())) and m[1].upper() in SQL_READ for s in statements)


def _valkey_read(line: str) -> bool:
    try:
        words = [w.upper() for w in shlex.split(line)]
    except ValueError:
        return False
    if words[0] in VALKEY_READ_SUBCOMMANDS:
        return len(words) > 1 and words[1] in VALKEY_READ_SUBCOMMANDS[words[0]]
    return words[0] in VALKEY_READ


def config_file(service) -> str:
    """The config file named in the Service's command (Valkey keeps requirepass there), or ''."""
    command = service.spec.get("command") or []
    words = shlex.split(command) if isinstance(command, str) else [str(w) for w in command]
    return next((w for w in words if w.endswith(".conf")), "")


def run(args, ctx) -> int:
    if args.write and args.statement is None:
        raise UsageError("--write only applies to a statement given with -e")
    service = ctx.declared.resolve_one(args.datastore)
    engine = service.datastore
    if engine is None:
        raise UsageError(f"{service.key} is not a Datastore (a MariaDB, Valkey or Postgres image)")
    script = ["sh", "-c", SCRIPTS[engine], "hops-db", config_file(service) if engine == "valkey" else ""]
    if args.statement is None:
        terminal.require_tty(f"hops db {service.key} without -e")
        return ctx.runner.attach("exec", "-it", docker.container_ref(ctx.runner, service), *script, tty=True)
    if not args.write and not read_only(engine, args.statement):
        allowed = "a read command" if engine == "valkey" else "SELECT, SHOW, EXPLAIN or DESCRIBE"
        raise UsageError(f"the -e statement is not {allowed}; add --write to run it")
    statement = args.statement if args.statement.endswith("\n") else args.statement + "\n"
    result = ctx.runner.docker("exec", "-i", docker.container_ref(ctx.runner, service), *script,
                               *ONE_SHOT_ARGS[engine], input=statement, check=False)
    ctx.stdout.write(result.stdout)
    ctx.stderr.write(result.stderr)
    return result.returncode
