"""Typed error hierarchy.

Every error surfaced to a user carries two things: what went wrong, and what to
do about it. A message without remediation makes the user guess, and guessing
about where personal financial data lives is exactly what this project must not
require.

Exit codes are stable and distinct so scripts can branch on them.
"""

from __future__ import annotations


class BetError(Exception):
    """Base for every error BET raises deliberately.

    Anything else reaching the top level is a bug and is reported as one.
    """

    exit_code = 1

    def __init__(self, message: str, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation


class ConfigError(BetError):
    """Configuration is missing, malformed, or internally inconsistent."""

    exit_code = 2


class DataLocationError(ConfigError):
    """A data path resolves somewhere it must never be.

    Its own class because it is the one configuration error with permanent
    consequences: a warehouse inside a git work tree can be committed, and a
    public repository cannot be un-published.
    """

    exit_code = 3


class DatabaseError(BetError):
    """The warehouse is unreachable, locked, or at an unexpected version."""

    exit_code = 4


class NotFoundError(BetError):
    """A requested record does not exist."""

    exit_code = 5


class UsageError(BetError):
    """The command was invoked in a way that cannot be satisfied."""

    exit_code = 64  # sysexits.h EX_USAGE
