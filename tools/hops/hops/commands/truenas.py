"""hops truenas call: a read-only passthrough to the TrueNAS middleware, for gaps in hops."""
import json
import re

from hops import output, truenas
from hops.errors import UsageError

HELP = "read-only TrueNAS middleware calls (JSON-RPC), for what other hops commands lack"
# Read methods by name: TrueNAS's conventions for reads, plus reads that don't follow them.
READ_SUFFIXES = (".query", ".get_instance", ".config")
READ_METHODS = {"system.info", "system.version", "pool.dataset.details"}
METHOD = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)+")


def register(subparsers) -> None:
    p = subparsers.add_parser("truenas", help=HELP, description=HELP + ".")
    sub = p.add_subparsers(dest="truenas_command", metavar="<command>", required=True)
    call = sub.add_parser(
        "call", help="call a read method and print its result, with credentials redacted",
        description="Call a TrueNAS read method: *.query, *.get_instance, *.config, or "
                    + ", ".join(sorted(READ_METHODS)) + ". Every other method is refused before "
                    "anything is sent. Credential-looking fields are printed as " + truenas.REDACTED + ".")
    call.add_argument("method", help="e.g. system.info or pool.query")
    call.add_argument("params", nargs="*", metavar="json-param",
                      help='one JSON value per positional parameter, e.g. \'[["name","=","tank"]]\'')
    output.add_json_flag(call)
    call.set_defaults(func=run)


def is_read(method: str) -> bool:
    return bool(METHOD.fullmatch(method)) and (method in READ_METHODS or method.endswith(READ_SUFFIXES))


def run(args, ctx) -> int:
    if not is_read(args.method):
        raise UsageError(f"{args.method} is not a read method; hops truenas call only reads "
                         f"(*.query, *.get_instance, *.config, {', '.join(sorted(READ_METHODS))})")
    params = []
    for text in args.params:
        try:
            params.append(json.loads(text))
        except json.JSONDecodeError as exc:
            raise UsageError(f"parameter is not JSON: {text!r} ({exc.msg})") from None
    result = ctx.truenas.call(args.method, *params)
    output.print_json(truenas.redact(result, provider=args.method.startswith("cloudsync.credentials.")),
                      ctx.stdout)
    return 0
