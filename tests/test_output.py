"""Tests for acpc.output module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acpc.output import OutputHandler, OutputMode, stderr


# ---------------------------------------------------------------------------
# stderr helper
# ---------------------------------------------------------------------------


def test_stderr_writes_prefixed_message(capsys: pytest.CaptureFixture[str]) -> None:
    stderr("hello world")
    captured = capsys.readouterr()
    assert captured.err == "[acpc] hello world\n"
    assert captured.out == ""


# ---------------------------------------------------------------------------
# TEXT mode
# ---------------------------------------------------------------------------


class TestTextMode:
    def test_agent_message_chunk_writes_to_stdout(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        handler = OutputHandler(mode=OutputMode.TEXT)
        handler.on_agent_message_chunk("hello ")
        handler.on_agent_message_chunk("world")
        captured = capsys.readouterr()
        assert captured.out == "hello world"

    def test_tool_call_writes_to_stderr(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        handler = OutputHandler(mode=OutputMode.TEXT)
        handler.on_tool_call("Read file.py", kind="read")
        captured = capsys.readouterr()
        assert "[acpc] tool: read:Read file.py" in captured.err
        assert captured.out == ""


# ---------------------------------------------------------------------------
# JSON mode
# ---------------------------------------------------------------------------


class TestJsonMode:
    def test_on_event_writes_ndjson(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        handler = OutputHandler(mode=OutputMode.JSON)
        event = {"sessionUpdate": "agent_message_chunk", "text": "hi"}
        handler.on_event(event)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed == event

    def test_on_session_started_emits_meta_event(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        handler = OutputHandler(mode=OutputMode.JSON)
        handler.on_session_started("sess-123")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["acpc"] == "session_started"
        assert parsed["session_id"] == "sess-123"

    def test_on_session_ended_emits_meta_event(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        handler = OutputHandler(mode=OutputMode.JSON)
        handler.on_session_ended("sess-123", "end_turn", 0)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["acpc"] == "session_ended"
        assert parsed["session_id"] == "sess-123"
        assert parsed["stop_reason"] == "end_turn"
        assert parsed["exit_code"] == 0

    def test_agent_message_chunk_not_written_to_stdout(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        handler = OutputHandler(mode=OutputMode.JSON)
        handler.on_agent_message_chunk("should not appear")
        captured = capsys.readouterr()
        assert captured.out == ""


# ---------------------------------------------------------------------------
# QUIET mode
# ---------------------------------------------------------------------------


class TestQuietMode:
    def test_collects_chunks_and_writes_on_finalize(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        handler = OutputHandler(mode=OutputMode.QUIET)
        handler.on_agent_message_chunk("part1 ")
        handler.on_agent_message_chunk("part2")

        # Nothing on stdout yet
        mid = capsys.readouterr()
        assert mid.out == ""

        handler.finalize()
        captured = capsys.readouterr()
        assert captured.out == "part1 part2"


# ---------------------------------------------------------------------------
# Output file
# ---------------------------------------------------------------------------


class TestOutputFile:
    def test_output_file_written_on_success(self, tmp_path: Path) -> None:
        out_file = str(tmp_path / "result.md")
        handler = OutputHandler(mode=OutputMode.QUIET, output_file=out_file)
        handler.on_agent_message_chunk("final answer")
        handler.on_session_ended("s1", "end_turn", 0)
        handler.finalize()

        assert Path(out_file).read_text(encoding="utf-8") == "final answer"

    def test_output_file_not_written_on_error(self, tmp_path: Path) -> None:
        out_file = str(tmp_path / "result.md")
        handler = OutputHandler(mode=OutputMode.QUIET, output_file=out_file)
        handler.on_agent_message_chunk("partial")
        handler.on_session_error("s1", "boom")
        handler.finalize()

        assert not Path(out_file).exists()

    def test_output_file_written_in_text_mode(self, tmp_path: Path) -> None:
        out_file = str(tmp_path / "result.txt")
        handler = OutputHandler(mode=OutputMode.TEXT, output_file=out_file)
        handler.on_agent_message_chunk("streamed text")
        handler.on_session_ended("s1", "end_turn", 0)
        handler.finalize()

        # TEXT mode doesn't collect chunks, so output file is empty string
        assert Path(out_file).exists()
