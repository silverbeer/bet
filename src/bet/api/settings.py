"""API settings: the CLI's resolved configuration plus the worker count.

The warehouse location is not configured a second way. ``bet.config.resolve``
already applies the config file, ``BET_*`` variables and the git rule; a
parallel pydantic-settings layer would be a second resolution path that could
point the API at a different file than the CLI. ``Settings`` forbids unknown
``BET_*`` keys, so the worker count is read from ``WEB_CONCURRENCY`` -- the
variable uvicorn itself uses as its ``--workers`` default.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field

from bet.config import Settings, resolve
from bet.errors import ConfigError

WORKERS_ENV = "WEB_CONCURRENCY"


class ApiSettings(BaseModel):
    """Everything ``create_app`` needs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bet: Settings
    workers: Annotated[int, Field(ge=1)] = 1

    @classmethod
    def from_env(
        cls, *, env: Mapping[str, str] | None = None, config_path: Path | None = None
    ) -> Self:
        environ = env if env is not None else os.environ
        raw = environ.get(WORKERS_ENV, "").strip()
        try:
            workers = int(raw) if raw else 1
        except ValueError as exc:
            raise ConfigError(
                f"{WORKERS_ENV} is not an integer: {raw!r}",
                remediation=f"Unset {WORKERS_ENV} or set it to 1.",
            ) from exc
        settings = resolve(config_path=config_path, env=environ).settings
        return cls(bet=settings, workers=workers)
