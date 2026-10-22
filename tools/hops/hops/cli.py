"""The hops command line: parsing, the shared Context, and error reporting."""
import argparse
import os
import sys
from functools import cached_property
from pathlib import Path

from hops import commands, config
from hops.declared import Declared
from hops.errors import HopsError
from hops.runner import Runner

DESCRIPTION = """\
Ops and diagnostics for the Host, over SSH. hops changes runtime state only; declared state
(image tags and digests) changes only through a PR that doco-cd deploys (ADR 0001).

Targets: stack/service; a bare Service name when unique; a bare Stack name for all its Services."""


class Context:
    """What a command needs, each built on first use: a name error never reads .env or opens SSH."""

    def __init__(self, *, repo_root: Path | None = None, runner: Runner | None = None, env=None,
                 stdout=None, stderr=None):
        self.env = os.environ if env is None else env
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        if repo_root is not None:
            self.repo_root = repo_root
        if runner is not None:
            self.runner = runner

    @cached_property
    def repo_root(self) -> Path:
        return config.find_repo_root(env=self.env)

    @cached_property
    def declared(self) -> Declared:
        return Declared.load(self.repo_root, config.stack_root_name(self.env))

    @cached_property
    def runner(self) -> Runner:
        return Runner(config.load_host(self.repo_root, self.env))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hops", description=DESCRIPTION,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for module in commands.modules():
        module.register(sub)
    return parser


def main(argv: list[str] | None = None, *, ctx: Context | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = ctx or Context()
    try:
        return args.func(args, ctx)
    except HopsError as exc:
        ctx.stderr.write(f"hops: error: {exc}\n")
        return exc.exit_code
    except BrokenPipeError:  # e.g. `hops status | head`
        # Python flushes stdout again at exit; point it at devnull so that flush cannot fail.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
