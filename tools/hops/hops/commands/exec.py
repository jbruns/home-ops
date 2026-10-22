"""hops exec <svc> [-- cmd…]: run a command in a Service's container, or open a shell in it."""
import argparse

from hops import docker, terminal

HELP = "run a command in a Service's container (docker exec); without one, open an interactive shell"
# bash where the image has it, else sh.
SHELL = "if command -v bash >/dev/null 2>&1; then exec bash; fi; exec sh"


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "exec", help=HELP,
        description=HELP + ". With a command it runs non-interactively and returns the command's exit "
        "code. Without one it is Disruptive: it needs a TTY and uses ssh -t and docker exec -it.")
    p.add_argument("service", help="a Service: stack/service, or a unique Service name")
    p.add_argument("command", nargs=argparse.REMAINDER, help="the command and its arguments, after --")
    p.set_defaults(func=run)


def run(args, ctx) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    service = ctx.declared.resolve_one(args.service)
    if not command:
        terminal.require_tty(f"hops exec {service.key} without a command")
        return ctx.runner.attach("exec", "-it", docker.container_ref(ctx.runner, service),
                                 "sh", "-c", SHELL, tty=True)
    return ctx.runner.attach("exec", docker.container_ref(ctx.runner, service), *command)
