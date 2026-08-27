"""Tests for the session store: ids, meta lifecycle, liveness, rotation, cleanup.

Everything here asserts observable contract: what lands on disk, what a reader
sees, which operations are refused. Time is injected through `clock` so grace
windows and retention ages are exercised without sleeping.
"""

import json
import random
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from acpc import proc, sessions, vocab

BASE_TIME = 1_760_000_000.0


@pytest.fixture(autouse=True)
def state_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Every test gets its own throwaway state root."""
    root = tmp_path / "acpc-home"
    monkeypatch.setenv("ACPC_HOME", str(root))
    return root


def at(offset: float = 0.0) -> sessions.Clock:
    """A frozen clock `offset` seconds past the shared base time."""
    return lambda: BASE_TIME + offset


def make_session(
    *,
    prompt: str = "do the thing",
    name: str | None = None,
    entry: str = "builder",
    base_adapter: str = "codex",
    clock: sessions.Clock | None = None,
) -> sessions.SessionMeta:
    return sessions.create_session(
        entry=entry,
        base_adapter=base_adapter,
        prompt=prompt,
        resolution={"model": {"value": "gpt-5.6-luna", "source": "entry"}},
        target="builder~abc123",
        name=name,
        clock=clock or at(),
    )


def finished_session(*, state: str = "done", finished_offset: float = 60.0) -> str:
    meta = make_session(clock=at())
    sessions.mark_running(meta.session_id, pid=1, process_start_time="token", clock=at(1.0))
    sessions.transition(meta.session_id, state, exit_code=0, clock=at(finished_offset))
    return meta.session_id


class TestSessionIds:
    def test_ids_use_the_pinned_alphabet_and_length(self) -> None:
        for _ in range(20):
            session_id = sessions.allocate_session_id()
            assert sessions.SESSION_ID_PATTERN.match(session_id), session_id

    def test_ambiguous_glyphs_never_appear(self) -> None:
        drawn = "".join(sessions.allocate_session_id() for _ in range(50))
        assert not set(drawn) & set("01ol")

    def test_a_taken_id_is_re_rolled(self) -> None:
        # The same seed proposes the same first candidate, so the second call
        # can only succeed by detecting the collision and drawing again.
        first = sessions.allocate_session_id(rng=random.Random(20260805))
        second = sessions.allocate_session_id(rng=random.Random(20260805))
        assert second != first
        assert sessions.session_dir(first).is_dir()
        assert sessions.session_dir(second).is_dir()

    def test_allocation_claims_the_directory(self) -> None:
        session_id = sessions.allocate_session_id()
        assert sessions.session_dir(session_id).is_dir()


class TestCreateSession:
    def test_writes_the_advertised_layout_owner_only(self) -> None:
        meta = make_session(prompt="fix the failing test")

        directory = sessions.session_dir(meta.session_id)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert sessions.prompt_path(meta.session_id).read_text() == "fix the failing test"
        for path in (sessions.meta_path(meta.session_id), sessions.prompt_path(meta.session_id)):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_meta_carries_the_wire_contract_fields(self) -> None:
        meta = make_session(name="researcher")
        on_disk = json.loads(sessions.meta_path(meta.session_id).read_text())

        for key in (
            "session_id",
            "name",
            "entry",
            "base_adapter",
            "state",
            "pid",
            "process_start_time",
            "created_at",
            "started_at",
            "finished_at",
            "turns",
            "exit_code",
            "stop_reason",
            "tokens",
            "cost",
            "prompt_snippet",
            "resolution",
            "adapter_session_id",
            "target",
        ):
            assert key in on_disk, key
        assert on_disk["state"] == "starting"
        assert on_disk["state"] in vocab.SESSION_STATES
        assert on_disk["turns"] == 1
        assert on_disk["created_at"] == BASE_TIME
        assert on_disk["started_at"] is None
        assert on_disk["name"] == "researcher"

    def test_a_fresh_session_has_no_answer_yet(self) -> None:
        meta = make_session()
        assert not sessions.answer_path(meta.session_id).exists()

    def test_prompt_snippet_is_one_collapsed_line(self) -> None:
        meta = make_session(prompt="first line\n\n   second   line\t")
        assert sessions.read_meta(meta.session_id).prompt_snippet == "first line second line"

    def test_prompt_snippet_is_bounded(self) -> None:
        meta = make_session(prompt="x" * 5000)
        snippet = sessions.read_meta(meta.session_id).prompt_snippet
        assert len(snippet) <= 200
        assert snippet.endswith("…")

    def test_an_empty_prompt_is_stored_as_written(self) -> None:
        meta = make_session(prompt="")
        assert sessions.prompt_path(meta.session_id).read_text() == ""
        assert sessions.read_meta(meta.session_id).prompt_snippet == ""

    def test_session_paths_names_the_four_artifacts(self) -> None:
        meta = make_session()
        advertised = sessions.session_paths(meta.session_id)
        assert set(advertised) == {"dir", "prompt", "transcript", "answer"}
        assert advertised["dir"].endswith(meta.session_id)
        assert advertised["answer"].endswith("answer.md")


class TestTransitions:
    def test_running_stamps_started_at(self) -> None:
        meta = make_session()
        updated = sessions.mark_running(
            meta.session_id, pid=4711, process_start_time="tok", clock=at(5.0)
        )
        assert updated.state == "running"
        assert updated.started_at == BASE_TIME + 5.0
        assert updated.pid == 4711
        assert sessions.read_meta(meta.session_id).process_start_time == "tok"

    def test_finishing_stamps_finished_at_and_keeps_the_exit_code(self) -> None:
        meta = make_session()
        sessions.mark_running(meta.session_id, pid=4711, process_start_time="tok", clock=at(1.0))
        done = sessions.transition(
            meta.session_id,
            "done",
            exit_code=0,
            stop_reason="end_turn",
            tokens=41_000,
            clock=at(90.0),
        )
        assert done.state == "done"
        assert done.finished_at == BASE_TIME + 90.0
        assert done.exit_code == 0
        assert done.stop_reason == "end_turn"
        assert done.tokens == 41_000

    @pytest.mark.parametrize("final", ["done", "failed", "cancelled", "timeout", "orphaned"])
    def test_every_final_state_is_reachable_from_running(self, final: str) -> None:
        meta = make_session()
        sessions.mark_running(meta.session_id, pid=1, process_start_time="tok", clock=at(1.0))
        assert sessions.transition(meta.session_id, final, clock=at(2.0)).state == final

    def test_a_finished_session_cannot_transition_again(self) -> None:
        session_id = finished_session()
        with pytest.raises(sessions.SessionStateError, match="done"):
            sessions.transition(session_id, "running", clock=at(120.0))

    def test_a_running_session_cannot_go_back_to_starting(self) -> None:
        meta = make_session()
        sessions.mark_running(meta.session_id, pid=1, process_start_time="tok", clock=at(1.0))
        with pytest.raises(sessions.SessionStateError):
            sessions.transition(meta.session_id, "starting", clock=at(2.0))

    def test_an_unknown_state_is_rejected(self) -> None:
        meta = make_session()
        with pytest.raises(ValueError, match="unknown session state"):
            sessions.transition(meta.session_id, "wedged", clock=at(1.0))

    def test_update_meta_refuses_to_move_state(self) -> None:
        meta = make_session()
        with pytest.raises(ValueError, match="transition"):
            sessions.update_meta(meta.session_id, state="running")

    def test_update_meta_rejects_unknown_fields(self) -> None:
        meta = make_session()
        with pytest.raises(ValueError, match="unknown meta fields"):
            sessions.update_meta(meta.session_id, mood="cheerful")

    def test_update_meta_persists_known_fields(self) -> None:
        meta = make_session()
        sessions.update_meta(meta.session_id, adapter_session_id="acp-123", cost=0.42)
        stored = sessions.read_meta(meta.session_id)
        assert stored.adapter_session_id == "acp-123"
        assert stored.cost == 0.42


class TestLivenessAndOrphans:
    def test_a_live_host_process_keeps_the_session_running(self) -> None:
        meta = make_session()
        sessions.mark_running(
            meta.session_id,
            pid=proc.os.getpid(),
            process_start_time=proc.process_start_time(),
            clock=at(1.0),
        )
        assert sessions.load(meta.session_id, clock=at(600.0)).state == "running"

    def test_a_dead_host_process_is_reported_and_persisted_as_orphaned(self) -> None:
        meta = make_session()
        sessions.mark_running(
            meta.session_id, pid=_dead_pid(), process_start_time="tok", clock=at(1.0)
        )

        assert sessions.load(meta.session_id, clock=at(2.0)).state == "orphaned"
        on_disk = json.loads(sessions.meta_path(meta.session_id).read_text())
        assert on_disk["state"] == "orphaned"
        assert on_disk["stop_reason"] == "orphaned"
        assert on_disk["exit_code"] == vocab.EXIT_AGENT_ERROR
        assert on_disk["finished_at"] == BASE_TIME + 2.0

    def test_orphan_detection_does_not_wait_for_the_startup_grace(self) -> None:
        # Once a pid is recorded, liveness decides immediately — smoke kills a
        # freshly started session and expects `orphaned` within seconds.
        meta = make_session()
        sessions.mark_running(
            meta.session_id, pid=_dead_pid(), process_start_time="tok", clock=at(1.0)
        )
        assert sessions.load(meta.session_id, clock=at(1.5)).state == "orphaned"

    def test_orphan_detection_writes_the_placeholder_answer(self) -> None:
        meta = make_session()
        dead = _dead_pid()
        sessions.mark_running(meta.session_id, pid=dead, process_start_time="tok", clock=at(1.0))

        sessions.load(meta.session_id, clock=at(2.0))

        placeholder = sessions.answer_path(meta.session_id).read_text()
        assert str(dead) in placeholder
        assert meta.session_id in placeholder
        assert placeholder.count("\n") == 1

    def test_a_partial_answer_survives_orphan_detection(self) -> None:
        meta = make_session()
        sessions.write_answer(meta.session_id, "partial work\n")
        sessions.mark_running(
            meta.session_id, pid=_dead_pid(), process_start_time="tok", clock=at(1.0)
        )

        sessions.load(meta.session_id, clock=at(2.0))

        assert sessions.answer_path(meta.session_id).read_text() == "partial work\n"

    def test_pid_reuse_is_caught_by_the_identity_token(self) -> None:
        # The pid is alive (it is this test process) but the recorded kernel
        # start-time token belongs to a process that is gone.
        meta = make_session()
        sessions.mark_running(
            meta.session_id,
            pid=proc.os.getpid(),
            process_start_time="0",
            clock=at(1.0),
        )
        assert sessions.load(meta.session_id, clock=at(2.0)).state == "orphaned"

    def test_a_killed_process_flips_every_reader_to_orphaned(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            meta = make_session()
            sessions.mark_running(
                meta.session_id,
                pid=child.pid,
                process_start_time=proc.process_start_time(child.pid),
                clock=at(1.0),
            )
            assert sessions.load(meta.session_id, clock=at(2.0)).state == "running"
            child.kill()
            child.wait(timeout=10)

            assert sessions.load(meta.session_id, clock=at(3.0)).state == "orphaned"
            assert sessions.list_sessions(clock=at(3.0))[0].state == "orphaned"
            assert sessions.answer_path(meta.session_id).exists()
        finally:
            child.kill()
            child.wait(timeout=10)

    def test_within_the_startup_grace_a_pidless_session_stays_starting(self) -> None:
        meta = make_session()
        assert sessions.load(meta.session_id, clock=at(29.0)).state == "starting"

    def test_past_the_startup_grace_a_pidless_session_is_orphaned(self) -> None:
        meta = make_session()
        assert sessions.load(meta.session_id, clock=at(31.0)).state == "orphaned"

    def test_the_grace_window_runs_from_started_at_when_present(self) -> None:
        meta = make_session()
        sessions.update_meta(meta.session_id, started_at=BASE_TIME + 100.0)
        assert sessions.load(meta.session_id, clock=at(120.0)).state == "starting"
        assert sessions.load(meta.session_id, clock=at(140.0)).state == "orphaned"

    def test_a_finished_session_is_never_re_probed(self) -> None:
        session_id = finished_session()
        sessions.update_meta(session_id, pid=_dead_pid())
        assert sessions.load(session_id, clock=at(9999.0)).state == "done"

    def test_read_meta_reports_the_stored_state_verbatim(self) -> None:
        meta = make_session()
        sessions.mark_running(
            meta.session_id, pid=_dead_pid(), process_start_time="tok", clock=at(1.0)
        )
        assert sessions.read_meta(meta.session_id).state == "running"
        assert sessions.load(meta.session_id, clock=at(2.0)).state == "orphaned"


class TestTurnRotation:
    def test_rotation_parks_both_artifacts_and_counts_the_turn(self) -> None:
        session_id = finished_session()
        sessions.write_answer(session_id, "first answer")

        rotated = sessions.rotate_turn(session_id, clock=at(100.0))

        assert rotated.turns == 2
        assert sessions.turn_path(session_id, "prompt", 1).read_text() == "do the thing"
        assert sessions.turn_path(session_id, "answer", 1).read_text() == "first answer"
        assert not sessions.answer_path(session_id).exists()

    def test_rotation_resets_the_per_turn_result(self) -> None:
        session_id = finished_session()
        rotated = sessions.rotate_turn(session_id, clock=at(100.0))
        assert rotated.state == "starting"
        assert rotated.exit_code is None
        assert rotated.stop_reason is None
        assert rotated.finished_at is None
        assert rotated.pid is None

    def test_each_turn_is_renamed_exactly_once(self) -> None:
        session_id = finished_session()
        sessions.write_answer(session_id, "answer one")

        sessions.rotate_turn(session_id, clock=at(100.0))
        sessions.write_prompt(session_id, "second prompt")
        sessions.mark_running(session_id, pid=1, process_start_time="tok", clock=at(101.0))
        sessions.transition(session_id, "done", exit_code=0, clock=at(102.0))
        sessions.write_answer(session_id, "answer two")
        sessions.rotate_turn(session_id, clock=at(200.0))

        assert sessions.turn_path(session_id, "answer", 1).read_text() == "answer one"
        assert sessions.turn_path(session_id, "answer", 2).read_text() == "answer two"
        assert sessions.turn_path(session_id, "prompt", 2).read_text() == "second prompt"
        assert sessions.read_meta(session_id).turns == 3

    def test_rotation_never_overwrites_an_earlier_turn(self) -> None:
        # Crash-recovery shape: a rotation renamed the artifacts but died before
        # recording the new turn number, so the parked name is already taken.
        # Re-running it must not destroy the earlier turn's answer.
        session_id = finished_session()
        sessions.write_answer(session_id, "answer one")
        sessions.turn_path(session_id, "answer", 1).write_text("already parked")

        sessions.rotate_turn(session_id, clock=at(100.0))

        assert sessions.turn_path(session_id, "answer", 1).read_text() == "already parked"

    def test_rotation_is_refused_while_the_session_is_active(self) -> None:
        meta = make_session()
        sessions.mark_running(
            meta.session_id,
            pid=proc.os.getpid(),
            process_start_time=proc.process_start_time(),
            clock=at(1.0),
        )
        with pytest.raises(sessions.SessionStateError, match="running"):
            sessions.rotate_turn(meta.session_id, clock=at(2.0))

    def test_a_new_prompt_replaces_the_snippet(self) -> None:
        session_id = finished_session()
        sessions.rotate_turn(session_id, clock=at(100.0))
        sessions.write_prompt(session_id, "now apply the same fix to v2")
        assert sessions.read_meta(session_id).prompt_snippet == "now apply the same fix to v2"

    def test_rotation_clears_the_previous_failure(self) -> None:
        session_id = finished_session()
        sessions.update_meta(session_id, failure="the previous turn failed")

        rotated = sessions.rotate_turn(session_id, clock=at(100.0))

        assert rotated.failure is None

    def test_rotation_callbacks_see_the_locked_meta_snapshot(self) -> None:
        session_id = finished_session()
        current = sessions.read_meta(session_id)
        current.resolution["resolved"] = {
            "model": current.resolution.get("model", {}),
            "permissions": {
                "value": "edit",
                "source": "concurrent update",
            },
        }
        with sessions.session_lock(session_id):
            sessions.write_meta(current)

        rotated = sessions.rotate_turn(
            session_id,
            permissions_from_meta=lambda meta: meta.resolution["resolved"]["permissions"]["value"],
            clock=at(100.0),
        )

        assert rotated.resolution["resolved"]["permissions"]["value"] == "edit"

    def test_finalization_token_cannot_write_into_a_replacement_turn(self) -> None:
        session_id = finished_session()
        sessions.write_answer(session_id, "answer one")
        sessions.rotate_turn(session_id, prompt="replacement", clock=at(100.0))
        sessions.write_answer(session_id, "replacement answer")
        transcript_path = sessions.transcript_path(session_id)
        transcript_before = transcript_path.read_bytes() if transcript_path.exists() else None

        finalized = sessions.finalize_turn(
            session_id,
            "done",
            answer="stale answer",
            expected_turn=1,
            exit_code=0,
            clock=at(101.0),
            error_event={"message": "stale explanation"},
        )

        assert finalized is None
        assert sessions.read_meta(session_id).state == "starting"
        assert sessions.answer_path(session_id).read_text() == "replacement answer"
        if transcript_before is None:
            assert not transcript_path.exists()
        else:
            assert transcript_path.read_bytes() == transcript_before


class TestNamesAndSelectors:
    def test_a_free_name_needs_no_warning(self) -> None:
        assert sessions.claim_name("researcher", clock=at()) is None

    def test_rebinding_off_a_finished_session_warns(self) -> None:
        meta = make_session(name="researcher")
        sessions.mark_running(meta.session_id, pid=1, process_start_time="tok", clock=at(1.0))
        sessions.transition(meta.session_id, "done", exit_code=0, clock=at(2.0))

        warning = sessions.claim_name("researcher", clock=at(3.0))

        assert warning is not None
        assert meta.session_id in warning

    def test_rebinding_a_live_session_is_refused(self) -> None:
        meta = make_session(name="researcher")
        sessions.mark_running(
            meta.session_id,
            pid=proc.os.getpid(),
            process_start_time=proc.process_start_time(),
            clock=at(1.0),
        )
        with pytest.raises(sessions.SessionNameError, match="running"):
            sessions.claim_name("researcher", clock=at(2.0))

    def test_a_name_held_by_a_dead_session_is_free_again(self) -> None:
        meta = make_session(name="researcher")
        sessions.mark_running(
            meta.session_id, pid=_dead_pid(), process_start_time="tok", clock=at(1.0)
        )
        warning = sessions.claim_name("researcher", clock=at(2.0))
        assert warning is not None

    def test_last_is_reserved_as_a_name(self) -> None:
        with pytest.raises(sessions.SessionNameError, match="reserved"):
            sessions.claim_name("last", clock=at())

    def test_an_empty_name_is_refused(self) -> None:
        with pytest.raises(sessions.SessionNameError):
            sessions.claim_name("   ", clock=at())

    def test_a_session_id_resolves_to_itself(self) -> None:
        meta = make_session()
        assert sessions.resolve_selector(meta.session_id, clock=at()) == meta.session_id

    def test_a_name_resolves_to_its_session(self) -> None:
        meta = make_session(name="researcher")
        assert sessions.resolve_selector("researcher", clock=at()) == meta.session_id

    def test_a_rebound_name_resolves_to_the_newest_session(self) -> None:
        old = make_session(name="researcher", clock=at())
        sessions.mark_running(old.session_id, pid=1, process_start_time="tok", clock=at(1.0))
        sessions.transition(old.session_id, "done", exit_code=0, clock=at(2.0))
        new = make_session(name="researcher", clock=at(10.0))

        assert sessions.resolve_selector("researcher", clock=at(11.0)) == new.session_id

    def test_an_unknown_selector_is_not_found(self) -> None:
        with pytest.raises(sessions.SessionNotFound, match="nope"):
            sessions.resolve_selector("nope", clock=at())

    def test_last_is_rejected_without_a_tty(self) -> None:
        make_session()
        with pytest.raises(sessions.SessionNameError, match="TTY"):
            sessions.resolve_selector("last", clock=at())

    def test_last_resolves_to_the_newest_session_on_a_tty(self) -> None:
        make_session(clock=at())
        newest = make_session(clock=at(10.0))
        assert sessions.resolve_selector("last", allow_last=True, clock=at(11.0)) == (
            newest.session_id
        )

    def test_last_without_any_sessions_is_not_found(self) -> None:
        with pytest.raises(sessions.SessionNotFound):
            sessions.resolve_selector("last", allow_last=True, clock=at())


class TestListing:
    def test_an_empty_state_root_lists_nothing(self) -> None:
        assert sessions.list_sessions(clock=at()) == []

    def test_sessions_are_listed_newest_first(self) -> None:
        first = make_session(clock=at())
        second = make_session(clock=at(10.0))
        listed = [meta.session_id for meta in sessions.list_sessions(clock=at(20.0))]
        assert listed == [second.session_id, first.session_id]

    def test_listing_verifies_liveness(self) -> None:
        meta = make_session()
        sessions.mark_running(
            meta.session_id, pid=_dead_pid(), process_start_time="tok", clock=at(1.0)
        )
        assert sessions.list_sessions(clock=at(2.0))[0].state == "orphaned"

    def test_a_damaged_session_does_not_hide_the_others(self) -> None:
        healthy = make_session(clock=at())
        broken = make_session(clock=at(1.0))
        sessions.meta_path(broken.session_id).write_text("{not valid json")

        listed = [meta.session_id for meta in sessions.list_sessions(clock=at(2.0))]

        assert listed == [healthy.session_id]

    def test_runtime_uses_finished_at_once_the_session_ends(self) -> None:
        session_id = finished_session(finished_offset=61.0)
        meta = sessions.read_meta(session_id)
        assert sessions.runtime_seconds(meta, clock=at(9999.0)) == pytest.approx(60.0)

    def test_runtime_of_a_live_session_grows_with_the_clock(self) -> None:
        meta = make_session()
        sessions.mark_running(meta.session_id, pid=1, process_start_time="tok", clock=at(1.0))
        live = sessions.read_meta(meta.session_id)
        assert sessions.runtime_seconds(live, clock=at(31.0)) == pytest.approx(30.0)


class TestDamagedState:
    def test_unparseable_meta_names_the_file(self) -> None:
        meta = make_session()
        sessions.meta_path(meta.session_id).write_text("{not valid json")
        with pytest.raises(sessions.CorruptSessionError) as caught:
            sessions.read_meta(meta.session_id)
        assert str(sessions.meta_path(meta.session_id)) in str(caught.value)

    def test_a_state_outside_the_vocabulary_is_damage(self) -> None:
        meta = make_session()
        payload = json.loads(sessions.meta_path(meta.session_id).read_text())
        payload["state"] = "confused"
        sessions.meta_path(meta.session_id).write_text(json.dumps(payload))
        with pytest.raises(sessions.CorruptSessionError, match="confused"):
            sessions.read_meta(meta.session_id)

    def test_a_wrongly_typed_field_is_damage(self) -> None:
        meta = make_session()
        payload = json.loads(sessions.meta_path(meta.session_id).read_text())
        payload["turns"] = "many"
        sessions.meta_path(meta.session_id).write_text(json.dumps(payload))
        with pytest.raises(sessions.CorruptSessionError, match="turns"):
            sessions.read_meta(meta.session_id)

    def test_a_json_array_is_damage(self) -> None:
        meta = make_session()
        sessions.meta_path(meta.session_id).write_text("[]")
        with pytest.raises(sessions.CorruptSessionError):
            sessions.read_meta(meta.session_id)

    def test_a_missing_session_is_not_found(self) -> None:
        with pytest.raises(sessions.SessionNotFound, match="zzzz"):
            sessions.read_meta("zzzz")

    def test_unknown_keys_survive_a_round_trip(self) -> None:
        meta = make_session()
        payload = json.loads(sessions.meta_path(meta.session_id).read_text())
        payload["future_field"] = {"kept": True}
        sessions.meta_path(meta.session_id).write_text(json.dumps(payload))

        sessions.update_meta(meta.session_id, tokens=7)

        reread = json.loads(sessions.meta_path(meta.session_id).read_text())
        assert reread["future_field"] == {"kept": True}
        assert reread["tokens"] == 7


class TestLocking:
    def test_concurrent_writers_do_not_lose_updates(self) -> None:
        meta = make_session()
        session_id = meta.session_id
        rounds = 40

        def bump() -> None:
            for _ in range(rounds):
                with sessions.session_lock(session_id):
                    current = sessions.read_meta(session_id)
                    current.tokens += 1
                    sessions.write_meta(current)

        workers = [threading.Thread(target=bump) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)

        assert sessions.read_meta(session_id).tokens == 2 * rounds

    def test_the_lock_is_re_entrant_within_a_thread(self) -> None:
        meta = make_session()
        with sessions.session_lock(meta.session_id), sessions.session_lock(meta.session_id):
            sessions.update_meta(meta.session_id, tokens=3)
        assert sessions.read_meta(meta.session_id).tokens == 3

    def test_meta_is_never_read_torn(self) -> None:
        meta = make_session()
        session_id = meta.session_id
        stop = threading.Event()
        failures: list[str] = []

        def write() -> None:
            for index in range(200):
                sessions.update_meta(session_id, tokens=index)
            stop.set()

        def read() -> None:
            while not stop.is_set():
                try:
                    sessions.read_meta(session_id)
                except sessions.SessionError as error:  # pragma: no cover - failure path
                    failures.append(str(error))
                    return

        writer = threading.Thread(target=write)
        reader = threading.Thread(target=read)
        writer.start()
        reader.start()
        writer.join(timeout=20)
        reader.join(timeout=20)

        assert failures == []


class TestDeletion:
    def test_a_finished_session_is_deletable(self) -> None:
        session_id = finished_session()
        sessions.delete_session(session_id, clock=at(100.0))
        assert not sessions.session_dir(session_id).exists()

    def test_a_running_session_is_refused(self) -> None:
        meta = make_session()
        sessions.mark_running(
            meta.session_id,
            pid=proc.os.getpid(),
            process_start_time=proc.process_start_time(),
            clock=at(1.0),
        )
        with pytest.raises(sessions.SessionStateError, match="stop it before rm"):
            sessions.delete_session(meta.session_id, clock=at(2.0))
        assert sessions.session_dir(meta.session_id).exists()

    def test_an_orphaned_session_is_deletable(self) -> None:
        meta = make_session()
        sessions.mark_running(
            meta.session_id, pid=_dead_pid(), process_start_time="tok", clock=at(1.0)
        )
        sessions.delete_session(meta.session_id, clock=at(2.0))
        assert not sessions.session_dir(meta.session_id).exists()

    def test_deleting_an_unknown_session_is_not_found(self) -> None:
        with pytest.raises(sessions.SessionNotFound):
            sessions.delete_session("zzzz", clock=at())


class TestPrune:
    def test_old_finished_sessions_go(self) -> None:
        old = finished_session()
        removed = sessions.prune_sessions(older_than=86_400.0, clock=at(200_000.0))
        assert [meta.session_id for meta in removed] == [old]
        assert not sessions.session_dir(old).exists()

    def test_recent_sessions_stay(self) -> None:
        recent = finished_session()
        assert sessions.prune_sessions(older_than=86_400.0, clock=at(100.0)) == []
        assert sessions.session_dir(recent).exists()

    def test_age_is_measured_from_finished_at_not_created_at(self) -> None:
        # Created long ago, finished just now: the threshold must spare it.
        session_id = make_session(clock=at()).session_id
        sessions.mark_running(session_id, pid=1, process_start_time="tok", clock=at(1.0))
        sessions.transition(session_id, "done", exit_code=0, clock=at(500_000.0))

        assert sessions.prune_sessions(older_than=86_400.0, clock=at(500_100.0)) == []
        assert sessions.session_dir(session_id).exists()

    def test_dry_run_reports_without_deleting(self) -> None:
        old = finished_session()
        candidates = sessions.prune_sessions(older_than=86_400.0, dry_run=True, clock=at(200_000.0))
        assert [meta.session_id for meta in candidates] == [old]
        assert sessions.session_dir(old).exists()

    def test_running_sessions_are_never_touched(self) -> None:
        meta = make_session()
        sessions.mark_running(
            meta.session_id,
            pid=proc.os.getpid(),
            process_start_time=proc.process_start_time(),
            clock=at(1.0),
        )
        assert sessions.prune_sessions(older_than=1.0, clock=at(500_000.0)) == []
        assert sessions.session_dir(meta.session_id).exists()

    def test_orphaned_sessions_are_collected(self) -> None:
        meta = make_session()
        sessions.mark_running(
            meta.session_id, pid=_dead_pid(), process_start_time="tok", clock=at(1.0)
        )
        # Detection is what ends the session, so its age runs from there.
        assert sessions.load(meta.session_id, clock=at(2.0)).state == "orphaned"

        removed = sessions.prune_sessions(older_than=86_400.0, clock=at(500_000.0))

        assert [entry.session_id for entry in removed] == [meta.session_id]
        assert not sessions.session_dir(meta.session_id).exists()

    def test_a_just_detected_orphan_is_not_old_enough_to_prune(self) -> None:
        meta = make_session()
        sessions.mark_running(
            meta.session_id, pid=_dead_pid(), process_start_time="tok", clock=at(1.0)
        )
        assert sessions.prune_sessions(older_than=86_400.0, clock=at(500_000.0)) == []
        assert sessions.session_dir(meta.session_id).exists()


def _dead_pid() -> int:
    """A pid that is guaranteed not to be running."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait(timeout=10)
    return child.pid
