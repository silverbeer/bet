"""The application factory and the warehouse lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bet import __version__
from bet.api import auth, balances, bets, card, errors, health, me, similar
from bet.api.settings import WORKERS_ENV, ApiSettings
from bet.database.connection import connect
from bet.errors import ConfigError
from bet.services.auth import warm_up


def require_single_worker(settings: ApiSettings) -> None:
    """Refuse more than one worker: DuckDB allows one read-write process per file.

    A second worker would fail on the file lock at best. The lock remains the
    backstop for a worker count this check cannot see (``uvicorn --workers N``
    on the command line); this makes the configured case fail before serving.
    """
    if settings.workers != 1:
        raise ConfigError(
            f"the BET API must run with exactly one worker, not {settings.workers}.",
            remediation=(
                "DuckDB allows only one read-write process per warehouse file.\n"
                f"Unset {WORKERS_ENV} (or set it to 1) and start with `bet-api`."
            ),
        )


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    """Build the app.

    There is no app without a token signing key: ``ApiSettings`` requires one,
    and ``from_env`` raises a ``ConfigError`` naming the variable to set (see
    ``bet.api.settings``).
    """
    resolved = settings if settings is not None else ApiSettings.from_env()
    require_single_worker(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # create=False: the server never creates a warehouse. `bet init` (or
        # the deployment's init container) does, and applies migrations.
        with connect(resolved.bet, create=False) as conn:
            app.state.conn = conn
            # Before serving, so the first unknown-user login is not faster.
            warm_up()
            try:
                yield
            finally:
                del app.state.conn

    app = FastAPI(title="BET API", version=__version__, lifespan=lifespan)
    app.state.settings = resolved
    errors.install(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(bets.router)
    app.include_router(card.router)
    app.include_router(balances.router)
    app.include_router(similar.router)
    return app
