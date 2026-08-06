"""Behavioral tests for the public transcript format and cursor."""

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from acpc.transcript import SCHEMA, Transcript, TranscriptError, last_event_time


@pytest.fixture
def transcript_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state_root = tmp_path / "acpc-state"
    monkeypatch.setenv("ACPC_HOME", str(state_root))
    session = state_root / "sessions" / "abcd"
    session.mkdir(parents=True)
    return session / "transcript.ndjson"


def test_create_writes_version_header_and_owner_only_file(transcript_path: Path) -> None:
    transcript = Transcript(transcript_path)

    assert json.loads(transcript_path.read_text()) == {"schema": SCHEMA}
    assert stat.S_IMODE(transcript_path.stat().st_mode) == 0o600
    assert transcript.read() == ([], 0)


def test_last_event_time_returns_the_newest_complete_event_timestamp(
    transcript_path: Path,
) -> None:
    transcript = Transcript(transcript_path)
    transcript.append("msg", text="first", ts=1234.5)
    transcript.append("msg", text="last", ts=1250)

    assert last_event_time(transcript_path) == 1250.0


def test_last_event_time_returns_none_for_header_only_and_missing_files(
    transcript_path: Path,
) -> None:
    Transcript(transcript_path)

    assert last_event_time(transcript_path) is None
    assert last_event_time(transcript_path.with_name("missing.ndjson")) is None


def test_last_event_time_skips_a_torn_final_line(transcript_path: Path) -> None:
    transcript = Transcript(transcript_path)
    transcript.append("msg", text="complete", ts=1234)
    with transcript_path.open("ab") as file:
        file.write(b'{"i": 2, "ts": 9999, "type": "msg"')

    assert last_event_time(transcript_path) == 1234.0


def test_last_event_time_returns_none_for_a_garbage_final_line(transcript_path: Path) -> None:
    transcript = Transcript(transcript_path)
    transcript.append("msg", text="complete", ts=1234)
    with transcript_path.open("ab") as file:
        file.write(b"not-json\n")

    assert last_event_time(transcript_path) is None


def test_last_event_time_reads_only_the_tail_of_a_large_file(transcript_path: Path) -> None:
    transcript = Transcript(transcript_path)
    for index in range(100):
        transcript.append("msg", text="x" * 100, ts=float(index))

    assert transcript_path.stat().st_size > 8192
    assert last_event_time(transcript_path) == 99.0


def test_last_event_time_does_not_repair_a_torn_transcript(transcript_path: Path) -> None:
    transcript = Transcript(transcript_path)
    transcript.append("msg", text="complete", ts=1234)
    with transcript_path.open("ab") as file:
        file.write(b'{"i": 2, "ts": 9999, "type": "msg"')
    before = transcript_path.read_bytes()

    assert last_event_time(transcript_path) == 1234.0
    assert transcript_path.read_bytes() == before


def test_append_assigns_wire_fields_and_allows_unknown_fields(
    transcript_path: Path,
) -> None:
    transcript = Transcript(transcript_path, clock=lambda: 1234.5)

    first = transcript.append("msg", text="hello", source="agent")
    second = transcript.append({"type": "usage", "tokens": 42, "cost": 0.01, "i": 999, "ts": 1235})

    assert first == {"type": "msg", "text": "hello", "source": "agent", "ts": 1234.5, "i": 1}
    assert second["i"] == 2
    assert second["ts"] == 1235
    assert [event["i"] for event in transcript.read().events] == [1, 2]
    assert all(
        "i" in event and "ts" in event and "type" in event for event in transcript.read().events
    )
    assert transcript_path.read_bytes().count(b"\n") == 3


def test_since_and_tail_share_one_global_cursor_across_turns(transcript_path: Path) -> None:
    transcript = Transcript(transcript_path, clock=lambda: 10)
    transcript.append("state", **{"from": "starting", "to": "running"})
    transcript.append("msg", text="turn one")
    first_turn = transcript.read(since=0)

    transcript.append("state", **{"from": "running", "to": "done"})
    transcript.append("msg", text="turn two")
    second_turn = transcript.read(since=first_turn.next_cursor)
    tailed = transcript.read(since=0, tail=2)

    assert first_turn.next_cursor == 2
    assert [event["text"] for event in second_turn.events if event["type"] == "msg"] == ["turn two"]
    assert [event["i"] for event in second_turn.events] == [3, 4]
    assert [event["i"] for event in tailed.events] == [3, 4]
    assert tailed.next_cursor == 4
    assert transcript.read(since=4).next_cursor == 4


def test_empty_tail_and_since_past_end_do_not_advance_cursor(transcript_path: Path) -> None:
    transcript = Transcript(transcript_path, clock=lambda: 10)
    transcript.append("msg", text="only event")

    assert transcript.read(tail=0) == ([], 0)
    assert transcript.read(since=99) == ([], 99)


def test_reopening_an_existing_transcript_continues_its_cursor(transcript_path: Path) -> None:
    first = Transcript(transcript_path, clock=lambda: 10)
    first.append("msg", text="first instance")

    second = Transcript(transcript_path, clock=lambda: 11)
    second.append("error", message="second instance")

    assert [event["i"] for event in second.read().events] == [1, 2]
    assert second.read().events[-1]["ts"] == 11


@pytest.mark.skipif(os.name == "nt", reason="Windows chmod does not provide owner read/write bits")
def test_append_does_not_reread_or_chmod_an_open_transcript(transcript_path: Path) -> None:
    transcript = Transcript(transcript_path, clock=lambda: 10)
    transcript.append("msg", text="before permissions change")
    transcript_path.chmod(stat.S_IWUSR)

    try:
        appended = transcript.append("error", message="write-only append")
        assert stat.S_IMODE(transcript_path.stat().st_mode) == stat.S_IWUSR
    finally:
        transcript_path.chmod(0o600)

    assert appended["i"] == 2


def test_invalid_trailing_line_is_ignored_but_invalid_middle_line_is_not(
    transcript_path: Path,
) -> None:
    transcript = Transcript(transcript_path, clock=lambda: 1)
    transcript.append("msg", text="complete")
    with transcript_path.open("ab") as file:
        file.write(b'{"i": 2, "type": "msg"')

    page = transcript.read()
    assert [event["text"] for event in page.events] == ["complete"]
    assert page.next_cursor == 1

    transcript.append("error", message="recovered")
    lines = transcript_path.read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[-1])["i"] == 2

    bad_path = transcript_path.with_name("bad.ndjson")
    bad_path.write_bytes(
        b'{"schema": "acpc.transcript/1"}\n'
        b'{"i": 1, "ts": 1, "type": "msg", "text": "ok"}\n'
        b"not-json\n"
    )
    with pytest.raises(TranscriptError, match="bad.ndjson"):
        Transcript(bad_path).read()


def test_missing_or_wrong_schema_header_is_rejected(transcript_path: Path) -> None:
    missing = transcript_path.with_name("missing-header.ndjson")
    missing.write_text('{"i": 1}\n')
    with pytest.raises(TranscriptError, match="missing-header.ndjson"):
        Transcript(missing)

    wrong = transcript_path.with_name("wrong-schema.ndjson")
    wrong.write_text('{"schema": "acpc.transcript/2"}\n')
    with pytest.raises(TranscriptError, match="wrong-schema.ndjson"):
        Transcript(wrong)


def test_reader_keeps_unknown_event_shapes_readable(transcript_path: Path) -> None:
    transcript = Transcript(transcript_path, clock=lambda: 1)
    transcript.append("msg", text="known")
    with transcript_path.open("ab") as file:
        file.write(b'{"i": 2, "type": "future", "new_field": true}\n')

    page = Transcript(transcript_path).read()

    assert page.next_cursor == 2
    assert page.events[-1] == {"i": 2, "type": "future", "new_field": True}


def test_concurrent_appends_produce_one_contiguous_cursor(transcript_path: Path) -> None:
    transcript = Transcript(transcript_path, clock=lambda: 2)

    def append_one(number: int) -> None:
        transcript.append("msg", text=f"event {number}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append_one, range(64)))

    events = transcript.read().events
    assert [event["i"] for event in events] == list(range(1, 65))
    assert [event["type"] for event in events] == ["msg"] * 64
    assert all(event["text"].startswith("event ") for event in events)
