"""The file a bet was captured from.

A sportsbook export is trustworthy because it came from the sportsbook. A
screenshot read by an agent is not: it is a transcription, and transcriptions
are wrong occasionally and silently. ``SourceFile`` is what makes that
recoverable — the image is archived unchanged and content-addressed, so a
figure that looks wrong six months from now can be checked against the picture
it came from instead of being trusted or discarded on vibes.

Pairs with ``Bet.capture_method``: that says *how*, this says *from what*.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from bet.models.bet import CaptureMethod
from bet.models.ownership import AwareTimestamps, OwnedModel

# Chunked rather than read whole: a PDF statement can be large, and there is no
# reason for the hash of a file to depend on it fitting in memory.
_HASH_CHUNK = 1024 * 1024


def sha256_of(path: Path) -> str:
    """The content address of a file on disk."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class SourceFile(OwnedModel, AwareTimestamps):
    """One archived file that one or more bets were captured from."""

    id: UUID

    archived_path: str = Field(min_length=1)
    original_name: str = Field(min_length=1)

    sha256: str = Field(min_length=64, max_length=64)
    byte_size: int = Field(gt=0)
    media_type: str | None = None

    capture_method: CaptureMethod = "screenshot"
    notes: str | None = None

    @model_validator(mode="after")
    def _archived_path_is_relative(self) -> Self:
        """Reject an absolute path at the boundary.

        The archive moves with ``data_dir``. An absolute path stored here
        survives that move by continuing to point at the old location, which
        reads as a present, correct value and is not.
        """
        if Path(self.archived_path).is_absolute():
            raise ValueError("archived_path must be relative to the source archive directory")
        return self

    @model_validator(mode="after")
    def _digest_is_hex(self) -> Self:
        if not all(c in "0123456789abcdef" for c in self.sha256):
            raise ValueError("sha256 must be lowercase hexadecimal")
        return self


__all__ = ["SourceFile", "sha256_of"]
