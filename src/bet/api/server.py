"""The ``bet-api`` entry point: uvicorn with exactly one worker."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from bet import log
from bet.api.app import create_app
from bet.errors import BetError


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bet-api", description="Serve the BET warehouse over HTTP (one worker)."
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    args = parser.parse_args(argv)
    log.configure()

    # Built here rather than by uvicorn from an import string, so a refusal
    # (too many workers, bad config) surfaces before uvicorn starts.
    try:
        app = create_app()
    except BetError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.remediation:
            print(exc.remediation, file=sys.stderr)
        raise SystemExit(exc.exit_code) from exc
    uvicorn.run(app, host=args.host, port=args.port, workers=1)
