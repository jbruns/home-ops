"""hops truenas call: a read-only passthrough to the TrueNAS middleware, for gaps in hops."""
import json
import re

from hops import output, truenas
from hops.errors import UsageError

HELP = "read-only TrueNAS middleware calls (JSON-RPC), for what other hops commands lack"
# Read methods by name: TrueNAS's conventions for reads, plus reads that don't follow them.
READ_SUFFIXES = (".query", ".get_instance", ".config")
READ_METHODS = {"system.info", "system.version", "pool.dataset.details"}
# Methods whose results carry free-form user settings (app config, instance environment, VM
# arguments) that redaction by field name cannot see into.
FREE_FORM = ("app.", "virt.", "vm.")
# TrueNAS query-options (api/*/common.py QueryOptions). `select` may rename fields with
# [source, alias] pairs, which would print a secret under a harmless name: only strings are allowed.
QUERY_OPTIONS = {"extra", "order_by", "select", "count", "get", "offset", "limit", "force_sql_filters"}
READS = ", ".join(f"*{m}" for m in READ_SUFFIXES) + ", " + ", ".join(sorted(READ_METHODS))
METHOD = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)+")


def register(subparsers) -> None:
    p = subparsers.add_parser("truenas", help=HELP, description=HELP + ".")
    sub = p.add_subparsers(dest="truenas_command", metavar="<command>", required=True)
    call = sub.add_parser(
        "call", help="call a read method and print its result, with credentials redacted",
        description=f"Call a TrueNAS read method ({READS}). Every other method is refused before "
                    f"anything is sent. Credential-looking fields are printed as {truenas.REDACTED}.")
    call.add_argument("method", help="e.g. system.info or pool.query")
    call.add_argument("params", nargs="*", metavar="json-param",
                      help='one JSON value per positional parameter, e.g. \'[["name","=","tank"]]\'')
    output.add_json_flag(call)
    call.set_defaults(func=run)


def is_read(method: str) -> bool:
    return bool(METHOD.fullmatch(method)) and (method in READ_METHODS or method.endswith(READ_SUFFIXES))


def check_query_options(options: dict) -> None:
    """Refuse query options hops can't vouch for: unknown keys and renaming `select` entries."""
    unknown = sorted(set(options) - QUERY_OPTIONS)
    if unknown:
        raise UsageError(f"unknown query option {', '.join(unknown)}; allowed: {', '.join(sorted(QUERY_OPTIONS))}")
    select = options.get("select", [])
    if not isinstance(select, list) or not all(isinstance(f, str) for f in select):
        raise UsageError("query option select takes field names only (strings); "
                         "[field, alias] pairs could print a secret under another name")


def run(args, ctx) -> int:
    if not is_read(args.method):
        raise UsageError(f"{args.method} is not a read method; hops truenas call only reads ({READS})")
    if args.method.startswith(FREE_FORM):
        raise UsageError(f"{args.method} is refused: {', '.join(p + '*' for p in FREE_FORM)} results hold "
                         "free-form settings that redaction cannot check")
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
