"""Archiving the file a bet was captured from.

The archive is content-addressed: a file lands at
``<sha256[:2]>/<sha256><ext>`` under ``Settings.source_archive_dir``. That
makes re-archiving the same screenshot idempotent — the second attempt finds
the bytes already there and reuses the existing row — and it means a filename
collision between two different phone screenshots called ``IMG_4417.PNG``
cannot overwrite one with the other.

The original is copied, never moved. A user pointing at a file in their
Photos library expects to still have it afterwards.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from bet.errors import UsageError
from bet.models.source import SourceFile, sha256_of

if TYPE_CHECKING:
    from bet.database.repository import Warehouse
    from bet.models.bet import CaptureMethod

# Extensions BET knows how to store and later show a human. Anything else is
# accepted too — refusing an unusual container would lose evidence for no gain
# — but only these get a media type recorded.
MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".heic": "image/heic",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}


def archive(
    w: Warehouse,
    path: Path,
    *,
    archive_dir: Path,
    capture_method: CaptureMethod = "screenshot",
) -> SourceFile:
    """Copy ``path`` into the archive and return its ``SourceFile`` row.

    Idempotent by content: archiving the same bytes twice returns the row
    written the first time, so re-running a capture does not duplicate the
    evidence or the bets that cite it.

    The row is written immediately rather than deferred into the caller's
    transaction. A source file is evidence, and evidence that exists on disk
    but not in the database is unfindable — worse than the reverse, where a
    row cites a file the archive can be re-checked for.
    """
    source = path.expanduser()
    if not source.is_file():
        raise UsageError(
            f"{path} is not a file.",
            remediation="Pass --source-file the path to the screenshot or statement.",
        )

    digest = sha256_of(source)
    existing = w.sources.by_digest(digest)
    if existing is not None:
        return existing

    suffix = source.suffix.lower()
    relative = Path(digest[:2]) / f"{digest}{suffix}"
    destination = archive_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(source, destination)

    return w.sources.add(
        SourceFile(
            tenant_id=w.scope.tenant_id,
            user_id=w.scope.user_id,
            id=uuid4(),
            archived_path=str(relative),
            original_name=source.name,
            sha256=digest,
            byte_size=source.stat().st_size,
            media_type=MEDIA_TYPES.get(suffix),
            capture_method=capture_method,
        )
    )


def resolve(archive_dir: Path, source_file: SourceFile) -> Path:
    """The absolute path of an archived file, for a caller that wants to open it."""
    return archive_dir / source_file.archived_path


__all__ = ["MEDIA_TYPES", "archive", "resolve"]
