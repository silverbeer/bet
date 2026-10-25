"""API settings: the CLI's resolved configuration, the worker count and token signing.

The warehouse location is not configured a second way. ``bet.config.resolve``
already applies the config file, ``BET_*`` variables and the git rule; a
parallel pydantic-settings layer would be a second resolution path that could
point the API at a different file than the CLI. ``Settings`` forbids unknown
``BET_*`` keys, so the worker count is read from ``WEB_CONCURRENCY`` -- the
variable uvicorn itself uses as its ``--workers`` default.

Token signing is read from ``BET_JWT_*``, a prefix ``bet.config`` skips, so
exporting these never affects the CLI and the key never enters ``Settings``:

* ``BET_JWT_SIGNING_KEY`` -- required, at least 32 bytes. Environment only,
  never the config file (a Kubernetes Secret in deployment).
* ``BET_JWT_ACCESS_TTL_SECONDS`` -- optional, default 900 (15 minutes).
* ``BET_JWT_REFRESH_TTL_SECONDS`` -- optional, default 2592000 (30 days).

Without a usable signing key the server refuses to start: every route but the
health checks needs one, and a pod that reports ready while refusing every
request is harder to diagnose than one that fails at startup with this reason.

The maintenance routes (``bet.api.maintenance``) are opened by a static token,
read from ``BET_MAINTENANCE_*`` -- also a prefix ``bet.config`` skips:

* ``BET_MAINTENANCE_TOKEN`` -- optional, at least 32 bytes. Unset or blank,
  the maintenance routes answer 404 as if they did not exist; set but short,
  the server refuses to start. It is not a user login: it opens no user route,
  and no user's token opens a maintenance route.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from bet.config import Settings, resolve
from bet.errors import ConfigError
from bet.services.auth import MIN_SIGNING_KEY_BYTES, TokenSettings

WORKERS_ENV = "WEB_CONCURRENCY"
SIGNING_KEY_ENV = "BET_JWT_SIGNING_KEY"
ACCESS_TTL_ENV = "BET_JWT_ACCESS_TTL_SECONDS"
REFRESH_TTL_ENV = "BET_JWT_REFRESH_TTL_SECONDS"
MAINTENANCE_TOKEN_ENV = "BET_MAINTENANCE_TOKEN"
MIN_MAINTENANCE_TOKEN_BYTES = 32

KEY_REMEDIATION = (
    f"Set {SIGNING_KEY_ENV} to a random value of at least {MIN_SIGNING_KEY_BYTES} bytes,\n"
    "for example the output of `openssl rand -base64 48`. It is read from the\n"
    "environment only, never from the config file."
)


class ApiSettings(BaseModel):
    """Everything ``create_app`` needs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bet: Settings
    auth: TokenSettings
    workers: Annotated[int, Field(ge=1)] = 1
    # None: the maintenance routes are disabled and answer 404.
    maintenance_token: SecretStr | None = None

    @classmethod
    def from_env(
        cls, *, env: Mapping[str, str] | None = None, config_path: Path | None = None
    ) -> Self:
        environ = env if env is not None else os.environ
        workers = _integer(environ, WORKERS_ENV, f"Unset {WORKERS_ENV} or set it to 1.")
        settings = resolve(config_path=config_path, env=environ).settings
        return cls(
            bet=settings,
            auth=token_settings(environ),
            workers=1 if workers is None else workers,
            maintenance_token=maintenance_token(environ),
        )


def _integer(environ: Mapping[str, str], name: str, remediation: str) -> int | None:
    raw = environ.get(name, "").strip()
    try:
        return int(raw) if raw else None
    except ValueError as exc:
        raise ConfigError(f"{name} is not an integer: {raw!r}", remediation=remediation) from exc


def token_settings(environ: Mapping[str, str]) -> TokenSettings:
    """Read ``BET_JWT_*``. Refuses a missing or short key, without ever echoing it."""
    key = environ.get(SIGNING_KEY_ENV, "")
    if not key.strip():
        raise ConfigError(f"{SIGNING_KEY_ENV} is not set.", remediation=KEY_REMEDIATION)

    lifetimes: dict[str, timedelta] = {}
    for field, name in (("access_ttl", ACCESS_TTL_ENV), ("refresh_ttl", REFRESH_TTL_ENV)):
        seconds = _integer(environ, name, f"Unset {name} or set it to a number of seconds.")
        if seconds is not None:
            lifetimes[field] = timedelta(seconds=seconds)

    try:
        return TokenSettings(signing_key=SecretStr(key), **lifetimes)
    except ValidationError as exc:
        # Messages only: a validation error's input would be the key itself.
        problems = "; ".join(str(e["msg"]) for e in exc.errors())
        raise ConfigError(
            f"the token settings are invalid: {problems}",
            remediation=(
                f"{KEY_REMEDIATION}\n{ACCESS_TTL_ENV} and {REFRESH_TTL_ENV} must be positive."
            ),
        ) from None


def maintenance_token(environ: Mapping[str, str]) -> SecretStr | None:
    """Read ``BET_MAINTENANCE_TOKEN``: None when unset, refused when short, never echoed."""
    token = environ.get(MAINTENANCE_TOKEN_ENV, "").strip()
    if not token:
        return None
    if len(token.encode()) < MIN_MAINTENANCE_TOKEN_BYTES:
        raise ConfigError(
            f"{MAINTENANCE_TOKEN_ENV} is shorter than {MIN_MAINTENANCE_TOKEN_BYTES} bytes.",
            remediation=(
                f"Set {MAINTENANCE_TOKEN_ENV} to a random value of at least "
                f"{MIN_MAINTENANCE_TOKEN_BYTES} bytes, for example the output of\n"
                f"`openssl rand -base64 48`, or unset it to disable the maintenance routes."
            ),
        )
    return SecretStr(token)
