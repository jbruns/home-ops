"""hops truenas call: a read-only passthrough to the TrueNAS middleware, for gaps in hops."""
import json
import re

from hops import output, truenas
from hops.errors import UsageError

HELP = "read-only TrueNAS middleware calls (JSON-RPC), for what other hops commands lack"
# Read methods by name: TrueNAS's conventions for reads, plus reads that don't follow them.
READ_SUFFIXES = (".query", ".get_instance", ".config")
READ_METHODS = {"system.info", "system.version", "pool.dataset.details", "alert.list"}
# Fail closed: only these namespaces' reads, plus READ_METHODS, suited to Host-hygiene gaps and
# checked against the 25.10 schema for secret-bearing fields. Anything else may hold secrets that
# TrueNAS doesn't mask (SNMP communities, webhooks, tunables, scripts, credentials).
NAMESPACES = {
    "pool", "pool.dataset", "pool.snapshot", "pool.snapshottask", "pool.scrub", "disk", "boot",
    "boot.environment", "replication", "cloudsync", "service", "interface",
}
# Query options that only filter, order or count. `select` is refused: a projection can drop the
# fields TrueNAS's own masking keys on, or rename a secret. `extra` is refused: it switches on
# per-method data (e.g. disk.query's SED passwords).
QUERY_OPTIONS = {"order_by", "count", "get", "offset", "limit"}
README = "tools/hops/README.md (truenas call)"
READS = ", ".join(f"*{m}" for m in READ_SUFFIXES) + ", " + ", ".join(sorted(READ_METHODS))
METHOD = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)+")


def register(subparsers) -> None:
    p = subparsers.add_parser("truenas", help=HELP, description=HELP + ".")
    sub = p.add_subparsers(dest="truenas_command", metavar="<command>", required=True)
    call = sub.add_parser(
        "call", help="call a read method and print its result, with credentials redacted",
        description=f"Call a TrueNAS read method ({READS}) in an allowed namespace (see {README}). "
                    f"Every other method is refused before anything is sent. Credential-looking fields are printed as {truenas.REDACTED}.")
    call.add_argument("method", help="e.g. system.info or pool.query")
    call.add_argument("params", nargs="*", metavar="json-param",
                      help='one JSON value per positional parameter, e.g. \'[["name","=","tank"]]\'')
    output.add_json_flag(call)
    call.set_defaults(func=run)


def is_read(method: str) -> bool:
    return bool(METHOD.fullmatch(method)) and (method in READ_METHODS or method.endswith(READ_SUFFIXES))


def is_allowed(method: str) -> bool:
    return method in READ_METHODS or method.rsplit(".", 1)[0] in NAMESPACES


def check_query_options(options: dict) -> None:
    """Refuse query options other than filtering, ordering and counting, before connecting."""
    refused = sorted(set(options) - QUERY_OPTIONS)
    if refused:
        raise UsageError(f"query option {', '.join(refused)} is refused (allowed: {', '.join(sorted(QUERY_OPTIONS))}); "
                         "select the fields you need from the output instead")


def run(args, ctx) -> int:
    if not is_read(args.method):
        raise UsageError(f"{args.method} is not a read method; hops truenas call only reads ({READS})")
    if not is_allowed(args.method):
        raise UsageError(f"{args.method} is not on hops truenas call's allowlist, as its results may hold "
                         f"secrets; the allowed namespaces are listed in {README}")
    params = []
    for text in args.params:
        try:
            params.append(json.loads(text))
        except json.JSONDecodeError as exc:
            raise UsageError(f"parameter is not JSON: {text!r} ({exc.msg})") from None
    for param in params:
        if isinstance(param, dict):
            check_query_options(param)
    result = ctx.truenas.call(args.method, *params)
    output.print_json(truenas.redact(result, args.method), ctx.stdout)
    return 0
