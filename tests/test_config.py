"""Configuration resolution, precedence, and the git-work-tree safety rule."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bet.config import OutputFormat, Source, resolve
from bet.errors import ConfigError, DataLocationError


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory guaranteed not to be inside a git work tree."""
    target = tmp_path / "data"
    target.mkdir()
    return target


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git work tree with a data directory inside it."""
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


# ------------------------------------------------------------- precedence


def test_defaults_apply_when_nothing_is_set(tmp_path: Path) -> None:
    resolved = resolve(config_path=tmp_path / "missing.toml", env={})
    assert resolved.settings.default_format is OutputFormat.TABLE
    assert resolved.source_of("default_format") is Source.DEFAULT


def test_config_file_overrides_defaults(tmp_path: Path, outside: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{outside}"\ndefault_format = "csv"\n')

    resolved = resolve(config_path=config, env={})
    assert resolved.settings.default_format is OutputFormat.CSV
    assert resolved.source_of("default_format") is Source.CONFIG_FILE


def test_environment_overrides_config_file(tmp_path: Path, outside: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('default_format = "csv"\n')

    resolved = resolve(
        config_path=config,
        env={"BET_DEFAULT_FORMAT": "json", "BET_DATA_DIR": str(outside)},
    )
    assert resolved.settings.default_format is OutputFormat.JSON
    assert resolved.source_of("default_format") is Source.ENV


def test_cli_overrides_everything(tmp_path: Path, outside: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('default_format = "csv"\n')

    resolved = resolve(
        config_path=config,
        env={"BET_DEFAULT_FORMAT": "json", "BET_DATA_DIR": str(outside)},
        cli_overrides={"default_format": "table"},
    )
    assert resolved.settings.default_format is OutputFormat.TABLE
    assert resolved.source_of("default_format") is Source.CLI


def test_nested_thresholds_resolve_from_environment(tmp_path: Path, outside: Path) -> None:
    resolved = resolve(
        config_path=tmp_path / "missing.toml",
        env={"BET_THRESHOLDS__MIN_SETTLED_BETS": "5", "BET_DATA_DIR": str(outside)},
    )
    assert resolved.settings.thresholds.min_settled_bets == 5
    assert resolved.source_of("thresholds.min_settled_bets") is Source.ENV


# ------------------------------------------------------------- derived paths


def test_paths_derive_from_data_dir(tmp_path: Path, outside: Path) -> None:
    resolved = resolve(config_path=tmp_path / "missing.toml", env={"BET_DATA_DIR": str(outside)})
    settings = resolved.settings
    assert settings.db_path == outside / "bet.duckdb"
    assert settings.source_archive_dir == outside / "sources"
    assert settings.backup_dir == outside / "backups"


# ------------------------------------------------------- the git safety rule


def test_rejects_data_dir_inside_a_git_work_tree(tmp_path: Path, repo: Path) -> None:
    with pytest.raises(DataLocationError) as caught:
        resolve(config_path=tmp_path / "missing.toml", env={"BET_DATA_DIR": str(repo / "data")})
    assert "git work tree" in caught.value.message
    assert caught.value.remediation is not None


def test_rejects_a_symlink_that_points_into_a_git_work_tree(tmp_path: Path, repo: Path) -> None:
    link = tmp_path / "innocent"
    link.symlink_to(repo / "data")
    with pytest.raises(DataLocationError):
        resolve(config_path=tmp_path / "missing.toml", env={"BET_DATA_DIR": str(link)})


def test_rejects_a_relative_path_that_escapes_and_re_enters(tmp_path: Path, repo: Path) -> None:
    sneaky = repo / "data" / ".." / ".." / "repo" / "data"
    with pytest.raises(DataLocationError):
        resolve(config_path=tmp_path / "missing.toml", env={"BET_DATA_DIR": str(sneaky)})


def test_rejects_a_derived_path_even_when_data_dir_is_clean(
    tmp_path: Path, outside: Path, repo: Path
) -> None:
    """db_path is checked in its own right, not merely inherited."""
    with pytest.raises(DataLocationError) as caught:
        resolve(
            config_path=tmp_path / "missing.toml",
            env={"BET_DATA_DIR": str(outside), "BET_DB_PATH": str(repo / "data" / "b.duckdb")},
        )
    assert "db_path" in caught.value.message


def test_accepts_a_clean_path(tmp_path: Path, outside: Path) -> None:
    resolved = resolve(config_path=tmp_path / "missing.toml", env={"BET_DATA_DIR": str(outside)})
    assert resolved.settings.data_dir == outside.resolve()


# ------------------------------------------------------------------ errors


def test_malformed_toml_is_reported_with_remediation(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("this is not = = toml\n")
    with pytest.raises(ConfigError) as caught:
        resolve(config_path=config, env={})
    assert caught.value.remediation is not None


def test_unknown_key_is_rejected(tmp_path: Path, outside: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{outside}"\nnot_a_setting = 1\n')
    with pytest.raises(ConfigError):
        resolve(config_path=config, env={})
