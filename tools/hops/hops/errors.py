"""Errors hops reports as one line on stderr instead of a traceback."""


class HopsError(Exception):
    """A failure on the Host or in a command; exit code 1."""

    exit_code = 1


class UsageError(HopsError):
    """Bad input caught before anything touches the Host; exit code 2."""

    exit_code = 2


class ConfigError(UsageError):
    """Missing or invalid configuration (.env, environment, repo checkout)."""


class ResolveError(UsageError):
    """A target name that is unknown or ambiguous in the declared Stacks."""


class HostTimeout(HopsError):
    """A Host call ran past its deadline; the ssh process was ended."""
