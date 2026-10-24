"""``POST /sources``: archive a screenshot, for a write to cite by id.

The image is the raw request body, sent with its own ``Content-Type`` --
``image/png``, ``image/jpeg``, ``image/heic``, ``image/webp`` or ``image/gif``
-- rather than a multipart form, which would need a dependency for one field.
``filename`` is the name the file had where it was taken, kept because a
phone's screenshot name carries its capture time; its extension must agree with
the ``Content-Type``. Without one the file is named ``upload`` plus that
type's extension.

The answer is the ``source_file_id`` that ``POST /bets``, ``POST /balances``
and ``POST /cash-flows`` cite, with ``capture_method`` ``screenshot`` on the
row, exactly as ``bet add --capture-method screenshot --source-file`` archives
it.

The archive is content-addressed and its rows are per user. The same bytes
uploaded again by the same user return the same id (200, ``already_archived``);
the same bytes from another user become a row of that user's own (201), so the
answer never says whether anybody else holds that image.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel

from bet.api.auth import UNAUTHORIZED
from bet.api.deps import CurrentScope, Cursor
from bet.api.errors import PayloadTooLargeError, UnsupportedMediaTypeError
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.bet import CaptureMethod
from bet.services.sources import SourceToArchive, archive_upload
from bet.sources import MEDIA_TYPES

router = APIRouter(tags=["sources"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# Each image type the archive records, with the extensions that name it.
IMAGE_TYPES: dict[str, tuple[str, ...]] = {}
for _extension, _media_type in MEDIA_TYPES.items():
    if _media_type.startswith("image/"):
        IMAGE_TYPES[_media_type] = (*IMAGE_TYPES.get(_media_type, ()), _extension)


@dataclass(frozen=True)
class Upload:
    data: bytes
    media_type: str


class UploadedSource(BaseModel):
    """The archived file's row. ``already_archived`` when this user had sent these bytes."""

    source_file_id: UUID
    sha256: str
    byte_size: int
    media_type: str | None
    original_name: str
    capture_method: CaptureMethod
    already_archived: bool


def _too_large() -> PayloadTooLargeError:
    return PayloadTooLargeError(
        f"the upload is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB.",
        remediation="Send the screenshot itself, not a scan or a video.",
    )


async def upload_body(request: Request) -> Upload:
    """The request body, refused before it is read if its type or declared size is wrong."""
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type not in IMAGE_TYPES:
        raise UnsupportedMediaTypeError(
            f"{media_type!r} is not an image type BET archives."
            if media_type
            else "the upload has no Content-Type.",
            remediation="Send the image as the request body with Content-Type "
            + ", ".join(sorted(IMAGE_TYPES))
            + ".",
        )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise _too_large()

    chunks: list[bytes] = []
    size = 0
    # Counted as it arrives: a missing or wrong Content-Length must not let
    # an oversized body into memory.
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise _too_large()
        chunks.append(chunk)
    if size == 0:
        raise UsageError("the upload is empty.", remediation="Send the image's bytes as the body.")
    return Upload(data=b"".join(chunks), media_type=media_type)


def original_name(filename: str | None, media_type: str) -> str:
    """The final component of ``filename``, whose extension must match ``media_type``."""
    extensions = IMAGE_TYPES[media_type]
    if filename is None:
        return f"upload{extensions[0]}"
    name = Path(filename).name
    if Path(name).suffix.lower() not in extensions:
        raise UsageError(
            f"{filename!r} does not name a {media_type} file.",
            remediation=f"Send the file's own name, ending in {' or '.join(extensions)}, "
            "or omit filename.",
        )
    return name


IMAGE_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            media_type: {"schema": {"type": "string", "format": "binary"}}
            for media_type in sorted(IMAGE_TYPES)
        },
    }
}


@router.post(
    "/sources",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=IMAGE_BODY,
    responses=UNAUTHORIZED
    | {
        200: {"description": "Already archived for this user: the existing row"},
        400: {"description": "Empty body, or a filename that disagrees with the type"},
        413: {"description": f"Larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB"},
        415: {"description": "Not an image type BET archives"},
    },
)
def upload(
    request: Request,
    response: Response,
    scope: CurrentScope,
    cursor: Cursor,
    body: Annotated[Upload, Depends(upload_body)],
    filename: Annotated[
        str | None,
        Query(min_length=1, max_length=255, description="The file's own name, e.g. IMG_4417.PNG."),
    ] = None,
) -> UploadedSource:
    """Archive the image in the body and return the id a write cites it by."""
    archive_dir: Path | None = request.app.state.settings.bet.source_archive_dir
    assert archive_dir is not None  # Settings derives it from data_dir
    archived = archive_upload(
        Warehouse(cursor, scope),
        SourceToArchive(
            archive_dir=archive_dir,
            data=body.data,
            original_name=original_name(filename, body.media_type),
        ),
    )
    if not archived.is_new:
        response.status_code = status.HTTP_200_OK
    row = archived.source_file
    return UploadedSource(
        source_file_id=row.id,
        sha256=row.sha256,
        byte_size=row.byte_size,
        media_type=row.media_type,
        original_name=row.original_name,
        capture_method=row.capture_method,
        already_archived=not archived.is_new,
    )
