"""Provenance: whether a write may cite a source, and archiving the one it cites.

A write cites its evidence one of two ways: an already-archived
``source_file_id`` on its request, or a file handed to the service to archive
first -- a path on this machine, or uploaded bytes. Either way the archive is
content-addressed (``bet.sources``), so the same screenshot always lands on the
same row.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import CaptureMethod
from bet.models.source import SourceFile
from bet.services._query import Query
from bet.sources import archive, archive_bytes


class SourceToArchive(Query):
    """A file to archive and cite: a ``path`` here, or uploaded ``data``.

    ``archive_dir`` is where the archive lives -- server configuration, not
    something a caller chooses per request.
    """

    archive_dir: Path
    path: Path | None = None
    data: bytes | None = None
    original_name: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.path is None) == (self.data is None):
            raise ValueError("give exactly one of path or data")
        if self.data is not None and self.original_name is None:
            raise ValueError("uploaded data needs its original_name")
        return self


def check_provenance(
    capture_method: CaptureMethod,
    *,
    source: SourceToArchive | None,
    source_file_id: UUID | None,
    what: str = "bet",
) -> None:
    """Refuse a source that contradicts the capture method, or cannot be read.

    ``manual`` means a human typed the numbers. A file it was captured from is
    a contradiction, not extra detail. A path that is not a file fails here,
    before any write, rather than from inside the write transaction where it
    would surface as a rollback rather than as bad input.
    """
    if source is not None and source_file_id is not None:
        raise UsageError(
            "a source can be archived or cited by id, not both.",
            remediation="Pass the file to archive, or the source_file_id it was archived as.",
        )
    if source is not None or source_file_id is not None:
        refuse_manual_source(capture_method, what=what)
    if source is not None and source.path is not None:
        require_file(source.path)


def refuse_manual_source(capture_method: CaptureMethod, *, what: str = "bet") -> None:
    """A source file contradicts ``manual``: a human typed those numbers."""
    if capture_method == "manual":
        raise UsageError(
            "--source-file contradicts --capture-method manual.",
            remediation=f"A manually typed {what} was not captured from a file. Pass "
            "--capture-method screenshot (or pdf, statement, export) alongside it.",
        )


def require_file(path: Path) -> None:
    if not path.expanduser().is_file():
        raise UsageError(
            f"{path} is not a file.",
            remediation="Pass --source-file the path to the screenshot or statement.",
        )


def cited_source(w: Warehouse, source_file_id: UUID | None) -> SourceFile | None:
    """The archived source a request cites, if it cites one this scope owns.

    Another user's source is ``NotFoundError``, exactly as their bet would be.
    """
    return w.sources.get(source_file_id) if source_file_id is not None else None


def archive_source(
    w: Warehouse, source: SourceToArchive, *, capture_method: CaptureMethod
) -> SourceFile:
    """Archive ``source`` and return its row -- the existing one for known bytes."""
    if source.path is not None:
        return archive(
            w, source.path, archive_dir=source.archive_dir, capture_method=capture_method
        )
    assert source.data is not None and source.original_name is not None  # _one_source
    return archive_bytes(
        w,
        source.data,
        original_name=source.original_name,
        archive_dir=source.archive_dir,
        capture_method=capture_method,
    )


class ArchivedSource(BaseModel):
    """An uploaded file's row, and whether this upload is what archived it."""

    model_config = ConfigDict(frozen=True)

    source_file: SourceFile
    is_new: bool


def archive_upload(w: Warehouse, source: SourceToArchive) -> ArchivedSource:
    """Archive uploaded ``data`` on its own, for a later write to cite by id.

    Recorded as a ``screenshot``: an upload is an image of a slip or a screen.
    Known bytes return the scope's existing row. Both the row and the lookup
    are per user (``source_file_is_unique_per_user``), so ``is_new`` says
    whether *this* user had archived the bytes -- never whether anyone else had.
    """
    assert source.data is not None, "an upload carries data, not a path"
    existing = w.sources.by_digest(hashlib.sha256(source.data).hexdigest())
    if existing is not None:
        return ArchivedSource(source_file=existing, is_new=False)
    return ArchivedSource(
        source_file=archive_source(w, source, capture_method="screenshot"), is_new=True
    )


def source_id(
    w: Warehouse,
    *,
    source: SourceToArchive | None,
    source_file_id: UUID | None,
    capture_method: CaptureMethod,
) -> UUID | None:
    """The id a new row cites: the archived ``source``, or ``source_file_id`` as given.

    Called inside the write transaction, after :func:`check_provenance` and
    :func:`cited_source` have passed.
    """
    if source is not None:
        return archive_source(w, source, capture_method=capture_method).id
    return source_file_id


__all__ = [
    "ArchivedSource",
    "SourceToArchive",
    "archive_source",
    "archive_upload",
    "check_provenance",
    "cited_source",
    "refuse_manual_source",
    "require_file",
    "source_id",
]
