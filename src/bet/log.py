"""structlog configuration.

One rule governs everything here:

    **stdout is for results. Logs go to stderr.**

A command emitting ``--format json`` is emitting a document another program will
parse. A stray log line on stdout corrupts it, and the corruption is silent —
the consumer sees malformed JSON, not a warning. Routing every log to stderr
makes that class of bug impossible rather than merely unlikely.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

import structlog

_CONTEXT_KEYS = ("command", "import_run_id", "sportsbook", "user_id")


def configure(
    *, verbose: bool = False, json_logs: bool | None = None, stream: TextIO | None = None
) -> None:
    """Configure structlog for this process.

    ``json_logs`` defaults to "whatever is not a terminal": humans reading a
    terminal get the readable renderer, pipes and CI get JSON.
    """
    sink = stream if stream is not None else sys.stderr
    if json_logs is None:
        json_logs = not sink.isatty()

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sink.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.INFO
        ),
        # Never PrintLoggerFactory(sys.stdout): see the module docstring.
        logger_factory=structlog.PrintLoggerFactory(file=sink),
        cache_logger_on_first_use=False,
    )


def bind(**values: Any) -> None:
    """Bind standard context for the remainder of the command.

    Unknown keys are accepted — this is a convenience, not a schema — but the
    documented ones are what every command is expected to set.
    """
    structlog.contextvars.bind_contextvars(**{k: v for k, v in values.items() if v is not None})


def clear() -> None:
    structlog.contextvars.clear_contextvars()


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


__all__ = ["_CONTEXT_KEYS", "bind", "clear", "configure", "get_logger"]
