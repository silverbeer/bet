"""Typed configuration with layered resolution and per-value provenance.

Precedence, lowest to highest:

    defaults  ->  config file  ->  environment  ->  CLI flags

Every resolved value remembers which layer supplied it, so ``bet config show``
can answer "why is it this?" rather than only "what is it?". A user debugging a
path that points somewhere unexpected needs the source, not the value.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bet.errors import ConfigError
from bet.paths import (
    assert_outside_git,
    default_config_path,
    default_data_dir,
    default_state_dir,
)

ENV_PREFIX = "BET_"


class Source(StrEnum):
    """Where a resolved configuration value came from."""

    DEFAULT = "default"
    CONFIG_FILE = "config file"
    ENV = "environment"
    CLI = "cli flag"


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    CSV = "csv"


class Thresholds(BaseModel):
    """Statistical thresholds governing when a finding may be reported.

    Defaults are deliberately conservative. Small samples are not hidden, but
    they are labelled exploratory rather than presented as findings (SB-688).
    """

    model_config = ConfigDict(extra="forbid")

    min_settled_bets: Annotated[int, Field(ge=1)] = 30
    min_total_risk: Annotated[float, Field(ge=0)] = 100.0
    exploratory_below: Annotated[int, Field(ge=1)] = 100


class Settings(BaseModel):
    """Fully resolved BET configuration."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    data_dir: Path = Field(default_factory=default_data_dir)
    db_path: Path | None = None
    source_archive_dir: Path | None = None
    backup_dir: Path | None = None
    state_dir: Path = Field(default_factory=default_state_dir)

    default_format: OutputFormat = OutputFormat.TABLE
    thresholds: Thresholds = Field(default_factory=Thresholds)

    @model_validator(mode="after")
    def _derive_and_validate_paths(self) -> Self:
        """Fill unset paths from ``data_dir``, then enforce the git rule.

        Derivation happens before validation so a caller that sets only
        ``data_dir`` still gets every dependent path checked.
        """
        object.__setattr__(self, "data_dir", self.data_dir.expanduser())
        if self.db_path is None:
            object.__setattr__(self, "db_path", self.data_dir / "bet.duckdb")
        if self.source_archive_dir is None:
            object.__setattr__(self, "source_archive_dir", self.data_dir / "sources")
        if self.backup_dir is None:
            object.__setattr__(self, "backup_dir", self.data_dir / "backups")

        for field in ("data_dir", "db_path", "source_archive_dir", "backup_dir"):
            value = getattr(self, field)
            assert value is not None
            object.__setattr__(self, field, assert_outside_git(value, field=field))
        return self


# --------------------------------------------------------------------- layers


def _from_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"config file is not valid TOML: {path}",
            remediation=f"Fix the syntax error and retry:\n    {exc}",
        ) from exc


def _coerce(key: str, raw: str) -> Any:
    """Interpret an environment string for the field it targets."""
    if key in {"data_dir", "db_path", "source_archive_dir", "backup_dir", "state_dir"}:
        return Path(raw)
    if key == "default_format":
        return raw
    return raw


def _from_env(env: Mapping[str, str]) -> dict[str, Any]:
    """Read ``BET_*`` variables.

    Nested thresholds use a double underscore: ``BET_THRESHOLDS__MIN_SETTLED_BETS``.
    """
    found: dict[str, Any] = {}
    for name, raw in env.items():
        if not name.startswith(ENV_PREFIX) or not raw:
            continue
        key = name[len(ENV_PREFIX) :].lower()
        if "__" in key:
            parent, _, child = key.partition("__")
            if parent == "thresholds":
                found.setdefault("thresholds", {})[child] = raw
            continue
        found[key] = _coerce(key, raw)
    return found


def _merge(
    base: dict[str, Any], overlay: Mapping[str, Any], source: Source, provenance: dict[str, Source]
) -> None:
    """Apply ``overlay`` onto ``base``, recording provenance one level deep."""
    for key, value in overlay.items():
        if isinstance(value, Mapping):
            # Record provenance per child whether or not the parent already
            # existed; a first-time nested overlay is still an overlay.
            current = base.get(key)
            nested = dict(current) if isinstance(current, Mapping) else {}
            for child, child_value in value.items():
                nested[child] = child_value
                provenance[f"{key}.{child}"] = source
            base[key] = nested
        else:
            base[key] = value
            provenance[key] = source


class ResolvedConfig(BaseModel):
    """Settings plus where each value came from."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: Settings
    sources: dict[str, Source]
    config_path: Path

    def source_of(self, key: str) -> Source:
        return self.sources.get(key, Source.DEFAULT)


def resolve(
    *,
    config_path: Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    """Resolve configuration across every layer, in precedence order."""
    path = config_path or default_config_path()
    environ = env if env is not None else os.environ

    merged: dict[str, Any] = {}
    provenance: dict[str, Source] = {}

    _merge(merged, _from_file(path), Source.CONFIG_FILE, provenance)
    _merge(merged, _from_env(environ), Source.ENV, provenance)
    _merge(
        merged,
        {k: v for k, v in (cli_overrides or {}).items() if v is not None},
        Source.CLI,
        provenance,
    )

    try:
        settings = Settings(**merged)
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(
            "configuration is invalid",
            remediation=f"Check {path} and any BET_* environment variables:\n    {exc}",
        ) from exc

    return ResolvedConfig(settings=settings, sources=provenance, config_path=path)


def write_config(path: Path, values: Mapping[str, Any]) -> None:
    """Write configuration to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {
        key: str(value) if isinstance(value, Path) else value for key, value in values.items()
    }
    with path.open("wb") as handle:
        tomli_w.dump(serialisable, handle)
