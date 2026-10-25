"""``bet init`` — create the local warehouse and apply migrations."""

from __future__ import annotations

import typer

from bet.cli.backend import refuse_remote
from bet.cli.context import options_from
from bet.cli.output import render
from bet.config import resolve, write_config
from bet.database import identity
from bet.database.connection import connect, on_disk_storage_version
from bet.services.bootstrap import bootstrap_warehouse


def init(ctx: typer.Context) -> None:
    """Create the data directories, the warehouse, and apply migrations.

    Safe to re-run: directories are created only if missing and migrations are
    skipped if already applied. The git-work-tree rule has already been enforced
    by configuration resolution before this runs, so there is no path here that
    creates a warehouse inside a repository.
    """
    resolved = resolve()
    refuse_remote(resolved, "bet init")
    settings = resolved.settings
    fmt = options_from(ctx).fmt

    created: list[dict[str, str]] = []
    for field in ("data_dir", "source_archive_dir", "backup_dir", "state_dir"):
        directory = getattr(settings, field)
        existed = directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        created.append(
            {
                "item": field,
                "path": str(directory),
                "action": "already present" if existed else "created",
            }
        )

    if not resolved.config_path.is_file():
        write_config(resolved.config_path, {"data_dir": str(settings.data_dir)})
        config_action = "created"
    else:
        config_action = "already present"
    created.append({"item": "config", "path": str(resolved.config_path), "action": config_action})

    assert settings.db_path is not None
    db_existed = settings.db_path.exists()
    with connect(settings) as conn:
        booted = bootstrap_warehouse(conn)

    scope, version = booted.scope, booted.version
    if booted.scope_created:
        identity.record_in_config(resolved, scope)

    created.append(
        {
            "item": "warehouse",
            "path": str(settings.db_path),
            "action": "already present" if db_existed else "created",
        }
    )
    created.append(
        {
            "item": "storage format",
            "path": str(on_disk_storage_version(settings.db_path) or "unknown"),
            "action": f"pinned {settings.storage_version}",
        }
    )
    created.append(
        {
            "item": "schema version",
            "path": f"{version:04d}" if version is not None else "none",
            "action": (
                f"applied {len(booted.applied)} of {booted.pending_before} pending"
                if booted.applied
                else "up to date"
            ),
        }
    )

    created.append(
        {
            "item": "local user",
            "path": str(scope.user_id),
            "action": "created" if booted.scope_created else "already present",
        }
    )

    render(created, columns=["item", "path", "action"], fmt=fmt, title="bet init")


__all__ = ["init"]
