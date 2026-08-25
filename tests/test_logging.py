"""structlog conventions — above all, that logs never reach stdout."""

from __future__ import annotations

import io
import json

import pytest

from bet import log


@pytest.fixture(autouse=True)
def _reset() -> None:
    log.clear()


def test_json_renderer_emits_parseable_lines() -> None:
    stream = io.StringIO()
    log.configure(json_logs=True, stream=stream)
    log.get_logger().info("imported", count=3)
    payload = json.loads(stream.getvalue())
    assert payload["event"] == "imported"
    assert payload["count"] == 3


def test_console_renderer_is_human_readable() -> None:
    stream = io.StringIO()
    log.configure(json_logs=False, stream=stream)
    log.get_logger().info("imported", count=3)
    assert "imported" in stream.getvalue()


def test_bound_context_appears_in_every_record() -> None:
    stream = io.StringIO()
    log.configure(json_logs=True, stream=stream)
    log.bind(command="import", sportsbook="fanduel", user_id="u-1")
    log.get_logger().info("started")
    payload = json.loads(stream.getvalue())
    assert payload["command"] == "import"
    assert payload["sportsbook"] == "fanduel"
    assert payload["user_id"] == "u-1"


def test_none_context_values_are_dropped() -> None:
    stream = io.StringIO()
    log.configure(json_logs=True, stream=stream)
    log.bind(command="roi", import_run_id=None)
    log.get_logger().info("started")
    assert "import_run_id" not in json.loads(stream.getvalue())


def test_debug_is_suppressed_unless_verbose() -> None:
    stream = io.StringIO()
    log.configure(verbose=False, json_logs=True, stream=stream)
    log.get_logger().debug("noisy")
    assert stream.getvalue() == ""

    stream = io.StringIO()
    log.configure(verbose=True, json_logs=True, stream=stream)
    log.get_logger().debug("noisy")
    assert "noisy" in stream.getvalue()


def test_logs_never_go_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """The contract SB-696 exists to guarantee."""
    log.configure(json_logs=True, stream=None)
    log.get_logger().info("should be on stderr")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "should be on stderr" in captured.err
