"""Capture provenance on ``bet add``: --capture-method and --source-file (SB-1049).

The property under test throughout is that a transcribed bet is *distinguishable*
from a typed one. Every assertion here exists because the alternative — a
screenshot-derived figure that reads like a hand-entered one — corrupts every
downstream ROI number silently and permanently.

bet-guard: synthetic-amounts
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from bet import sources
from bet.cli.main import app
from bet.config import Settings
from bet.database import identity
from bet.database.connection import connect
from bet.database.repository import Warehouse
from bet.errors import UsageError
from bet.models.source import SourceFile, sha256_of

runner = CliRunner()

# A one-pixel PNG. Real bytes, so hashing and copying are exercised for real,
# but obviously not a real bet slip — nothing here touches personal data.
ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63f8cfc0f01f00050001ff89993d1d"
    "0000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    for name in list(dict(os.environ)):
        if name.startswith("BET_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BET_DATA_DIR", str(data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def slip(tmp_path: Path) -> Path:
    """A stand-in for a photographed bet slip."""
    path = tmp_path / "IMG_4417.PNG"
    path.write_bytes(ONE_PIXEL_PNG)
    return path


@contextmanager
def _open_warehouse(data: Path) -> Iterator[Warehouse]:
    settings = Settings(data_dir=data)
    config = type("_Config", (), {"settings": settings})()
    with connect(settings) as conn:
        scope = identity.resolve_scope(conn, config)
        yield Warehouse(conn, scope)


def _add(*extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "fanduel",
            "--leg",
            "sport=NFL,market=spread,selection=Home -3.5,odds=-110",
            "--stake",
            "10.00",
            *extra,
        ],
    )


# ------------------------------------------------------------- the default


def test_a_typed_bet_is_still_manual(data_dir: Path) -> None:
    """The default must not move. Every existing invocation means the same thing."""
    runner.invoke(app, ["init"])
    result = _add()
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        bet = w.bets.current()[0]
        assert bet.capture_method == "manual"
        assert bet.source_file_id is None


# ------------------------------------------------------- the screenshot path


def test_a_screenshot_bet_is_tagged_and_cites_its_image(data_dir: Path, slip: Path) -> None:
    runner.invoke(app, ["init"])
    result = _add("--capture-method", "screenshot", "--source-file", str(slip))
    assert result.exit_code == 0, result.output

    with _open_warehouse(data_dir) as w:
        bet = w.bets.current()[0]
        assert bet.capture_method == "screenshot"
        assert bet.source_file_id is not None

        archived = w.sources.get(bet.source_file_id)
        assert archived.original_name == "IMG_4417.PNG"
        assert archived.media_type == "image/png"
        assert archived.byte_size == len(ONE_PIXEL_PNG)
        assert archived.sha256 == hashlib.sha256(ONE_PIXEL_PNG).hexdigest()


def test_the_image_is_copied_into_the_archive_unchanged(data_dir: Path, slip: Path) -> None:
    runner.invoke(app, ["init"])
    assert _add("--capture-method", "screenshot", "--source-file", str(slip)).exit_code == 0

    with _open_warehouse(data_dir) as w:
        archived = w.sources.fetch_all()[0]

    stored = sources.resolve(data_dir / "sources", archived)
    assert stored.read_bytes() == ONE_PIXEL_PNG
    assert slip.exists(), "the original must be copied, never moved"


def test_the_archive_is_content_addressed(data_dir: Path, slip: Path) -> None:
    """Two phone screenshots both called IMG_4417.PNG must not overwrite each other."""
    runner.invoke(app, ["init"])
    assert _add("--capture-method", "screenshot", "--source-file", str(slip)).exit_code == 0

    with _open_warehouse(data_dir) as w:
        archived = w.sources.fetch_all()[0]

    digest = hashlib.sha256(ONE_PIXEL_PNG).hexdigest()
    assert archived.archived_path == str(Path(digest[:2]) / f"{digest}.png")
    assert not Path(archived.archived_path).is_absolute()


def test_re_archiving_the_same_image_reuses_one_row(data_dir: Path, slip: Path) -> None:
    """Two bets read off one screenshot cite one archived file, not two copies."""
    runner.invoke(app, ["init"])
    for _ in range(2):
        assert _add("--capture-method", "screenshot", "--source-file", str(slip)).exit_code == 0

    with _open_warehouse(data_dir) as w:
        assert w.sources.count() == 1
        bets = w.bets.current()
        assert len(bets) == 2
        assert bets[0].source_file_id == bets[1].source_file_id


def test_a_renamed_copy_of_the_same_bytes_does_not_duplicate(data_dir: Path, slip: Path) -> None:
    runner.invoke(app, ["init"])
    assert _add("--capture-method", "screenshot", "--source-file", str(slip)).exit_code == 0

    twin = slip.parent / "screenshot-copy.png"
    twin.write_bytes(ONE_PIXEL_PNG)
    assert _add("--capture-method", "screenshot", "--source-file", str(twin)).exit_code == 0

    with _open_warehouse(data_dir) as w:
        assert w.sources.count() == 1


# ------------------------------------------------------------- incoherence


def test_a_source_file_on_a_manual_bet_is_refused(slip: Path) -> None:
    """A human typing numbers did not read them off a file. That is a contradiction."""
    runner.invoke(app, ["init"])
    result = _add("--source-file", str(slip))
    assert isinstance(result.exception, UsageError)


def test_an_unknown_capture_method_is_refused() -> None:
    runner.invoke(app, ["init"])
    result = _add("--capture-method", "telepathy")
    assert isinstance(result.exception, UsageError)


def test_a_missing_source_file_is_refused(tmp_path: Path) -> None:
    runner.invoke(app, ["init"])
    result = _add("--capture-method", "screenshot", "--source-file", str(tmp_path / "gone.png"))
    assert isinstance(result.exception, UsageError)


def test_a_transcribed_bet_without_an_image_warns_but_records(data_dir: Path) -> None:
    """The evidence may genuinely be gone. Losing the bet as well helps nobody."""
    runner.invoke(app, ["init"])
    result = _add("--capture-method", "screenshot")
    assert result.exit_code == 0, result.output
    assert "Warning" in result.output

    with _open_warehouse(data_dir) as w:
        bet = w.bets.current()[0]
        assert bet.capture_method == "screenshot"
        assert bet.source_file_id is None


def test_nothing_is_written_when_the_bet_itself_is_rejected(data_dir: Path, slip: Path) -> None:
    """A source file row must not outlive the bet that failed to be written."""
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "add",
            "--non-interactive",
            "--sportsbook",
            "fanduel",
            "--leg",
            "odds=-110",
            "--capture-method",
            "screenshot",
            "--source-file",
            str(slip),
        ],
    )
    assert result.exit_code != 0

    with _open_warehouse(data_dir) as w:
        assert w.sources.count() == 0
        assert w.bets.current() == []


# --------------------------------------------------------------- provenance


def test_bets_show_reports_the_source_file(data_dir: Path, slip: Path) -> None:
    runner.invoke(app, ["init"])
    assert _add("--capture-method", "screenshot", "--source-file", str(slip)).exit_code == 0

    with _open_warehouse(data_dir) as w:
        bet = w.bets.current()[0]
        recorded = w.bets.provenance(bet.id)

    assert recorded["capture_method"] == "screenshot"
    assert recorded["source_file_id"] == bet.source_file_id

    result = runner.invoke(app, ["--format", "json", "bets", "show", str(bet.id)])
    assert result.exit_code == 0, result.output
    assert str(bet.source_file_id) in result.output


def test_list_can_separate_transcribed_bets_from_typed_ones(data_dir: Path, slip: Path) -> None:
    runner.invoke(app, ["init"])
    assert _add().exit_code == 0
    assert _add("--capture-method", "screenshot", "--source-file", str(slip)).exit_code == 0

    with _open_warehouse(data_dir) as w:
        methods = sorted(b.capture_method for b in w.bets.current())

    assert methods == ["manual", "screenshot"]


# -------------------------------------------------------------- the model


def test_sha256_of_matches_hashlib(slip: Path) -> None:
    assert sha256_of(slip) == hashlib.sha256(ONE_PIXEL_PNG).hexdigest()


def test_an_absolute_archived_path_is_refused() -> None:
    """It would survive a data_dir move by pointing at the old location forever."""
    with pytest.raises(ValueError, match="relative"):
        SourceFile(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            archived_path="/absolute/elsewhere.png",
            original_name="elsewhere.png",
            sha256="a" * 64,
            byte_size=1,
        )


def test_a_non_hex_digest_is_refused() -> None:
    with pytest.raises(ValueError, match="hexadecimal"):
        SourceFile(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            archived_path="ab/abc.png",
            original_name="abc.png",
            sha256="z" * 64,
            byte_size=1,
        )


def test_an_empty_file_is_refused() -> None:
    """byte_size > 0 in the model and the schema: a zero-byte image is not evidence."""
    with pytest.raises(ValueError):
        SourceFile(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            id=uuid.uuid4(),
            archived_path="ab/abc.png",
            original_name="abc.png",
            sha256="a" * 64,
            byte_size=0,
        )


def test_money_is_untouched_by_the_capture_path(data_dir: Path, slip: Path) -> None:
    """Provenance must not perturb the figures it describes."""
    runner.invoke(app, ["init"])
    assert _add("--capture-method", "screenshot", "--source-file", str(slip)).exit_code == 0

    with _open_warehouse(data_dir) as w:
        bet = w.bets.current()[0]
        assert bet.cash_staked == Decimal("10.00")
        assert bet.bonus_staked == Decimal("0.00")
