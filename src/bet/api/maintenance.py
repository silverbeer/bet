"""Maintenance routes: a consistent backup of the warehouse, and fetching it.

The API process is the only one that may open the warehouse (ADR 0002), so a
backup has to be made inside it (``bet.services.backup``). These routes are for
the operator's jobs -- the nightly backup CronJob and the off-machine copy --
not for users:

* ``POST /maintenance/backup?keep_days=14`` writes
  ``bet-YYYYMMDDTHHMMSSZ.duckdb`` to the configured ``backup_dir`` (in the
  container, ``/data/backups``), then deletes backups more than ``keep_days``
  (1 to 365) old. Answers 201 with the file's name, size, sha256 and row counts.
* ``GET /maintenance/backups/latest`` returns the newest backup's bytes, with
  its sha256 in ``X-Backup-SHA256``.

Both take ``Authorization: Bearer <BET_MAINTENANCE_TOKEN>`` -- a static token,
compared in constant time, that is not a user login: a user's access token is
refused here, and the maintenance token is refused on every user route. With
``BET_MAINTENANCE_TOKEN`` unset the routes are disabled and answer 404, the
same body as a route that does not exist.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, SecretStr

from bet.api.deps import Cursor
from bet.api.errors import ROUTES_REMEDIATION
from bet.errors import AuthenticationError, NotFoundError
from bet.log import get_logger
from bet.services.backup import create_backup, file_digest, latest_backup

log = get_logger(__name__)

DEFAULT_KEEP_DAYS = 14
MAX_KEEP_DAYS = 365

TOKEN_REMEDIATION = "Send `Authorization: Bearer <token>` with the API's BET_MAINTENANCE_TOKEN."

# Its own scheme, so OpenAPI does not merge it with the user access token.
maintenance_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="MaintenanceToken",
    description="The API's BET_MAINTENANCE_TOKEN. Not a user access token.",
)


def require_maintenance(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(maintenance_bearer)],
) -> None:
    """404 when disabled; otherwise 401 unless the bearer is the maintenance token."""
    expected: SecretStr | None = request.app.state.settings.maintenance_token
    if expected is None:
        raise NotFoundError(f"no route {request.url.path}.", remediation=ROUTES_REMEDIATION)
    if credentials is None:
        raise AuthenticationError("a maintenance token is required.", remediation=TOKEN_REMEDIATION)
    if not hmac.compare_digest(
        credentials.credentials.encode(), expected.get_secret_value().encode()
    ):
        raise AuthenticationError("the maintenance token is wrong.", remediation=TOKEN_REMEDIATION)


router = APIRouter(
    prefix="/maintenance",
    tags=["maintenance"],
    dependencies=[Depends(require_maintenance)],
    responses={
        401: {"description": "Missing or wrong maintenance token"},
        404: {"description": "Maintenance disabled: BET_MAINTENANCE_TOKEN is unset"},
    },
)


class BackupMade(BaseModel):
    """The backup written, how to verify a copy of it, and what was pruned."""

    name: str
    byte_size: int
    sha256: str
    created_at: datetime
    schema_version: int | None
    # Rows per table in the backup, keyed ``schema.table``.
    tables: dict[str, int]
    total_rows: int
    # Older backups deleted after this one was written.
    pruned: list[str]


def _backup_dir(request: Request) -> Path:
    backup_dir: Path | None = request.app.state.settings.bet.backup_dir
    assert backup_dir is not None  # Settings derives it from data_dir
    return backup_dir


@router.post(
    "/backup",
    status_code=status.HTTP_201_CREATED,
    responses={503: {"description": "The backup could not be written"}},
)
def backup(
    request: Request,
    cursor: Cursor,
    keep_days: Annotated[
        int,
        Query(ge=1, le=MAX_KEEP_DAYS, description="Delete backups older than this many days."),
    ] = DEFAULT_KEEP_DAYS,
) -> BackupMade:
    """Write a consistent copy of the whole warehouse, then prune old copies."""
    made = create_backup(
        cursor,
        _backup_dir(request),
        now=datetime.now(UTC),
        storage_version=request.app.state.settings.bet.storage_version,
        keep_days=keep_days,
    )
    log.info(
        "backup written",
        name=made.name,
        byte_size=made.byte_size,
        total_rows=made.total_rows,
        pruned=len(made.pruned),
    )
    return BackupMade(
        name=made.name,
        byte_size=made.byte_size,
        sha256=made.sha256,
        created_at=made.created_at,
        schema_version=made.schema_version,
        tables=dict(made.row_counts),
        total_rows=made.total_rows,
        pruned=list(made.pruned),
    )


LATEST_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The newest backup's bytes; its sha256 is in X-Backup-SHA256",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    },
}


@router.get("/backups/latest", response_class=FileResponse, responses=LATEST_RESPONSES)
def latest(request: Request) -> FileResponse:
    """Download the newest backup."""
    path = latest_backup(_backup_dir(request))
    if path is None:
        raise NotFoundError(
            "there is no backup yet.",
            remediation="Make one with POST /maintenance/backup.",
        )
    _, sha256 = file_digest(path)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
        headers={"X-Backup-SHA256": sha256},
    )
