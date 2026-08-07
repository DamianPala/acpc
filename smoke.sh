#!/usr/bin/env bash
# smoke.sh -- executable acceptance contract for acpc 0.3, derived from SPEC.md.
#
# Ported from the MVP harness and re-derived against the current spec (footer
# streams, --tail, two-level help, exit codes, permission rules). Structured in
# sections mapped to PLAN.md slices: Stage 2 flips a section from "pending" to
# "ready" in SECTION_READY as its slice lands, and this suite becomes the gate.
# A pending section is skipped and reported, never failed.
#
# Runs the dev build via `uv run acpc` against a throwaway ACPC_HOME (mktemp),
# with the ACP-speaking mock agent from tests/mock_agent.py registered as the
# "mock" adapter -- never a real ~/.acpc, never the installed acpc.
#
# Keeps going after a failed assertion so one run reports every problem;
# exits 1 if anything failed, 0 otherwise (pending sections don't fail).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Exported so the exported `acpc` function below still resolves it inside `bash -c`.
export SCRIPT_DIR

# ==============================================================================
# Section readiness -- the PLAN.md slice map. Stage 2 flips values to "ready".
# ==============================================================================
declare -A SECTION_READY=(
    [S06-run]=ready         # sync run, session dir layout, -o/--quiet/--max-output/--json, exit codes
    [S07-daemon-bg]=ready # --bg, wait, SIGTERM detach, daemon status/stop, concurrency, orphans
    [S08-views]=ready       # status views, log views + footers + cursors
    [S09-continue]=ready  # continue: context, rotation, cross-turn cursor space, errors
    [S10-agents]=ready      # agents list/detail/--models/--commands/--check/init, install
    [S11-maintenance]=ready # stop semantics, rm, prune
    [S12-cli]=ready         # help contract, -V, TTY rules, hostile inputs
    [S13-permissions]=ready # permission tiers visible in log, bypass-mode guard (needs S06+S08)
    ["S16-skills"]=ready      # bundled skill list/detail views and JSON
)

section_ready() {
    # section_ready <key> -- true when the section's slice has landed
    [[ "${SECTION_READY[$1]:-pending}" == "ready" ]]
}

declare -A SECTION_RESULT=()

ACPC_HOME="$(mktemp -d "${TMPDIR:-/tmp}/acpc-smoke.home.XXXXXX")"
export ACPC_HOME
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/acpc-smoke.scratch.XXXXXX")"

PASS_COUNT=0
FAIL_COUNT=0
SECTION_FAILS_BEFORE=0
PTY_SKIPPED=0

acpc() {
    uv run --project "$SCRIPT_DIR" acpc "$@"
}
export -f acpc 2>/dev/null || true

progress() {
    printf '==> %s\n' "$*" >&2
}

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf 'FAIL: %s\n' "$1" >&2
}

assert_eq() {
    # assert_eq <description> <expected> <actual>
    if [[ "$2" == "$3" ]]; then
        pass
    else
        fail "$1: expected [$2] / got: [$3]"
    fi
}

assert_contains() {
    # assert_contains <description> <haystack> <needle>
    if [[ "$2" == *"$3"* ]]; then
        pass
    else
        fail "$1: expected output to contain [$3] / got: [$2]"
    fi
}

assert_not_contains() {
    # assert_not_contains <description> <haystack> <needle>
    if [[ "$2" != *"$3"* ]]; then
        pass
    else
        fail "$1: expected output NOT to contain [$3] / got: [$2]"
    fi
}

assert_true() {
    # assert_true <description> <0-or-nonzero-as-string>
    if [[ "$2" == "0" ]]; then
        pass
    else
        fail "$1"
    fi
}

assert_file() {
    if [[ -e "$2" ]]; then
        pass
    else
        fail "$1: expected file to exist / got: missing [$2]"
    fi
}

assert_mode() {
    # assert_mode <description> <path> <expected-octal-mode>
    local actual
    actual="$(stat -c '%a' "$2" 2>/dev/null || echo MISSING)"
    assert_eq "$1" "$3" "$actual"
}

assert_session_id() {
    # assert_session_id <description> <id> -- 4 chars, 32-glyph alphabet
    # (lowercase letters and digits minus 0/o/1/l), per SPEC.md.
    if [[ "$2" =~ ^[abcdefghijkmnpqrstuvwxyz23456789]{4}$ ]]; then
        pass
    else
        fail "$1: [$2] is not a 4-char id from the 32-glyph alphabet"
    fi
}

assert_json_valid() {
    if printf '%s' "$2" | python3 -m json.tool >/dev/null 2>&1; then
        pass
    else
        fail "$1: expected valid JSON / got: [$2]"
    fi
}

assert_ndjson_valid() {
    local ok=0 line
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if ! printf '%s' "$line" | python3 -c 'import json,sys; json.loads(sys.stdin.read())' \
            >/dev/null 2>&1; then
            ok=1
        fi
    done <<<"$2"
    if [[ $ok -eq 0 ]]; then
        pass
    else
        fail "$1: not every line was valid JSON"
    fi
}

# run_acpc <args...> -- captures stdout/stderr/exit code without tripping
# `set -e`. Results land in LAST_OUT / LAST_ERR / LAST_RC.
run_acpc() {
    local out err rc
    out="$(mktemp "${SCRATCH}/out.XXXXXX")"
    err="$(mktemp "${SCRATCH}/err.XXXXXX")"
    set +e
    acpc "$@" >"$out" 2>"$err"
    rc=$?
    set -e
    LAST_OUT="$(cat "$out")"
    LAST_ERR="$(cat "$err")"
    LAST_RC=$rc
    rm -f "$out" "$err"
}

json_field() {
    jq -r "$2" <<<"$1"
}

session_state() {
    run_acpc status "$1" --json
    json_field "$LAST_OUT" '.state'
}

wait_for_state() {
    # wait_for_state <id> <want-state> [timeout-s]
    local id="$1" want="$2" timeout_s="${3:-15}" start now state
    start=$(date +%s)
    while true; do
        state="$(session_state "$id")"
        if [[ "$state" == "$want" ]]; then
            return 0
        fi
        now=$(date +%s)
        if ((now - start >= timeout_s)); then
            return 1
        fi
        sleep 0.3
    done
}

begin_section() {
    # begin_section <key> <description> -- true if the section should run
    local key="$1" description="$2"
    if ! section_ready "$key"; then
        progress "SECTION ${key}: PENDING (slice not landed) -- ${description}"
        SECTION_RESULT[$key]="pending"
        return 1
    fi
    progress "SECTION ${key}: ${description}"
    SECTION_FAILS_BEFORE=$FAIL_COUNT
    return 0
}

end_section() {
    # end_section <key>
    local key="$1"
    if ((FAIL_COUNT > SECTION_FAILS_BEFORE)); then
        SECTION_RESULT[$key]="FAILED ($((FAIL_COUNT - SECTION_FAILS_BEFORE)) assertions)"
    else
        SECTION_RESULT[$key]="passed"
    fi
}

# shellcheck disable=SC2329 # invoked indirectly via `trap cleanup EXIT` below
cleanup() {
    # Best-effort: kill any worker still holding a pid before removing state,
    # so no session's process outlives the throwaway root it was writing to.
    local mp pid state
    if [[ -d "${ACPC_HOME}/sessions" ]]; then
        for mp in "${ACPC_HOME}"/sessions/*/meta.json; do
            [[ -f "$mp" ]] || continue
            state="$(jq -r '.state // empty' "$mp" 2>/dev/null || true)"
            pid="$(jq -r '.pid // empty' "$mp" 2>/dev/null || true)"
            if [[ "$state" == "running" || "$state" == "starting" ]] && [[ -n "$pid" ]]; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
    fi
    rm -rf "$ACPC_HOME" "$SCRATCH" 2>/dev/null || true
}
trap cleanup EXIT

# ==============================================================================
# Setup: register the ACP mock agent (and friends) in the throwaway ACPC_HOME.
# Entry schema per ARCHITECTURE.md "Key decisions".
# ==============================================================================
PYTHON_BIN="$(uv run --project "$SCRIPT_DIR" python -c 'import sys; print(sys.executable)')"
mkdir -p "${ACPC_HOME}/agents"

cat >"${ACPC_HOME}/agents/mock.toml" <<EOF
name = "Mock Agent"
author = "acpc tests"
command = "${PYTHON_BIN} ${SCRIPT_DIR}/tests/mock_agent.py"
install_command = "true"
home = "~/.mock"
home_env = "MOCK_HOME"
bypass_modes = ["yolo"]
efforts = ["low", "medium", "high", "xhigh"]

[presets]
fast = { model = "mock-haiku-4-5", effort = "high" }
standard = { model = "mock-sonnet-5", effort = "high" }
max = { model = "mock-opus-5", effort = "xhigh" }
EOF

cat >"${ACPC_HOME}/agents/builder.toml" <<EOF
extends = "mock"
description = "Implements a task against a plan."
model = "mock-opus-5"
effort = "xhigh"
permissions = "write"
EOF

cat >"${ACPC_HOME}/agents/explorer.toml" <<EOF
extends = "mock"
description = "Read-only exploration."
model = "mock-haiku-4-5"
effort = "low"
permissions = "read"
EOF

# loner: same adapter, different home => different daemon target. The orphan
# test kill -9s the process behind its session; on the daemon path that is the
# daemon serving the whole target, so the victim must not share a target with
# the other long-lived sessions.
cat >"${ACPC_HOME}/agents/loner.toml" <<EOF
extends = "mock"
description = "Isolated target for the orphan test."
home = "~/.mock-loner"
EOF

# stopper: the same reasoning as loner, for the `daemon stop` probe. That verb
# is target-wide by definition, so running it against `mock` would take SLOW1
# and SLOW2 down with it and S08 would later poll a session this section
# killed. It cannot share `loner` either, because the orphan test kills that
# target at roughly the same point.
cat >"${ACPC_HOME}/agents/stopper.toml" <<EOF
extends = "mock"
description = "Isolated target for the daemon stop probe."
home = "~/.mock-stopper"
EOF

cat >"${ACPC_HOME}/agents/phantom.toml" <<EOF
name = "Phantom Agent"
author = "acpc tests"
command = "definitely-not-installed-phantom-xyz"
install_command = "false"
EOF

progress "state root: ${ACPC_HOME}"
progress "dev build: $(run_acpc -V >/dev/null 2>&1; echo "exit ${LAST_RC:-?}")"
run_acpc -V
progress "acpc -V -> ${LAST_OUT:-<none>}"

# ==============================================================================
# Long-lived sessions dispatched up front (mock 'slow' scenario, ~64s) so log
# polling overlaps real work. Requires S07 (bg) + S08 (views).
# ==============================================================================
SLOW_MACHINERY=0
SLOW1_ID=""
SLOW1_CURSOR=0
SLOW1_SEEN="${SCRATCH}/slow1_seen_indices"
: >"$SLOW1_SEEN"

# poll_slow1 -- one incremental `log --json` poll of SLOW1, cross-checking
# cursor continuity: no event index emitted twice, none skipped. Events come on
# stdout (NDJSON, each carrying its index `i`); the footer is on stderr.
poll_slow1() {
    [[ $SLOW_MACHINERY -eq 1 ]] || return 0
    run_acpc log "$SLOW1_ID" --since "$SLOW1_CURSOR" --json
    [[ -z "$LAST_OUT" ]] && return 0
    local line i
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        i="$(jq -r '.i' <<<"$line")"
        if grep -qxF "$i" "$SLOW1_SEEN"; then
            fail "log cursor continuity: event $i emitted twice for SLOW1"
        else
            pass
            echo "$i" >>"$SLOW1_SEEN"
        fi
        if ((i > SLOW1_CURSOR)); then
            SLOW1_CURSOR=$i
        fi
    done <<<"$LAST_OUT"
}

# Guarded on every slice this block asserts through: bg/wait (S07), views
# (S08), continue-while-running (S09), rm/stop semantics (S11).
if section_ready S07-daemon-bg && section_ready S08-views \
    && section_ready S09-continue && section_ready S11-maintenance; then
    SLOW_MACHINERY=1
    progress "dispatching SLOW1/SLOW2/SLOW3 (mock 'slow' scenario, ~64s each)"

    run_acpc run mock "run the slow scenario for SLOW1" --bg --name smoke-slow1 --quiet
    assert_true "dispatch SLOW1 exits 0" "$LAST_RC"
    SLOW1_ID="$(head -n1 <<<"$LAST_OUT")"
    assert_session_id "SLOW1 id has the specced shape" "$SLOW1_ID"

    run_acpc run mock "run the slow scenario for SLOW2" --bg --name smoke-slow2 --quiet
    SLOW2_ID="$(head -n1 <<<"$LAST_OUT")"

    run_acpc run loner "run the slow scenario for SLOW3" --bg --name smoke-slow3 --quiet
    SLOW3_ID="$(head -n1 <<<"$LAST_OUT")"

    # --- SLOW3: orphaned detection (kill -9 the process behind the session).
    # SLOW3 runs on the isolated `loner` target so the kill cannot take the
    # daemon serving SLOW1/SLOW2 with it. -------------------------------------
    progress "SLOW3: orphaned detection (kill -9 the recorded pid)"
    sleep 2
    run_acpc status "$SLOW3_ID" --json
    assert_eq "SLOW3 is running before kill" "running" "$(json_field "$LAST_OUT" '.state')"
    SLOW3_PID="$(jq -r '.pid' "${ACPC_HOME}/sessions/${SLOW3_ID}/meta.json")"
    kill -9 "$SLOW3_PID"
    sleep 1

    run_acpc status "$SLOW3_ID" --json
    assert_eq "status <id> verifies liveness: orphaned after kill -9" "orphaned" \
        "$(json_field "$LAST_OUT" '.state')"
    SLOW3_META_STATE="$(jq -r '.state' "${ACPC_HOME}/sessions/${SLOW3_ID}/meta.json")"
    assert_eq "detected orphaned state is persisted back to meta.json" "orphaned" \
        "$SLOW3_META_STATE"

    run_acpc log "$SLOW3_ID"
    assert_contains "log footer (stderr) reports orphaned" "$LAST_ERR" "orphaned"

    run_acpc wait "$SLOW3_ID"
    assert_eq "wait on orphaned session exits 1" "1" "$LAST_RC"
    assert_file "orphaned session still has an answer.md placeholder" \
        "${ACPC_HOME}/sessions/${SLOW3_ID}/answer.md"

    # --- SLOW2: gates while running, wait --timeout, stop --------------------
    progress "SLOW2: continue/rm while running, wait --timeout, stop"

    run_acpc continue "$SLOW2_ID" "should be rejected"
    assert_eq "continue on a running session is an error" "2" "$LAST_RC"
    assert_contains "continue-while-running error mentions running" "$LAST_ERR" "running"

    run_acpc rm "$SLOW2_ID"
    assert_eq "rm on a running session is a usage error" "2" "$LAST_RC"
    assert_contains "rm-while-running error suggests stop" "$LAST_ERR" "stop"

    t0=$(date +%s)
    run_acpc wait "$SLOW2_ID" --timeout 2
    t1=$(date +%s)
    assert_eq "wait --timeout gives up with exit 124" "124" "$LAST_RC"
    assert_true "wait --timeout returns promptly (<=6s)" "$(((t1 - t0) <= 6 ? 0 : 1))"
    run_acpc status "$SLOW2_ID" --json
    assert_eq "wait --timeout leaves the session running" "running" \
        "$(json_field "$LAST_OUT" '.state')"

    run_acpc stop "$SLOW2_ID"
    assert_eq "stop exits 0" "0" "$LAST_RC"
    run_acpc status "$SLOW2_ID" --json
    assert_eq "stop leaves the session cancelled" "cancelled" "$(json_field "$LAST_OUT" '.state')"
    assert_file "partial answer on disk after stop" "${ACPC_HOME}/sessions/${SLOW2_ID}/answer.md"

    run_acpc wait "$SLOW2_ID"
    assert_eq "wait mirrors the cancelled session result: exit 130" "130" "$LAST_RC"

    poll_slow1
fi

# ==============================================================================
# S06-run: run sync, session dir layout, -o, --quiet, --max-output, --json,
#          --dry-run, exit codes, on-disk contract
# ==============================================================================
if begin_section S06-run "run sync + session dir layout + output contract + exit codes"; then
    run_acpc run mock "smoke test sync run" --quiet
    assert_eq "sync run exits 0" "0" "$LAST_RC"
    assert_contains "sync run stdout carries the answer (quotes the prompt)" "$LAST_OUT" \
        "smoke test sync run"
    assert_eq "--quiet suppresses the stderr summary" "" "$LAST_ERR"

    run_acpc run mock "smoke test sync run with summary"
    assert_eq "sync run (no --quiet) exits 0" "0" "$LAST_RC"
    assert_contains "stderr summary is one line prefixed --" "$LAST_ERR" "-- "
    assert_contains "stderr summary carries the exit status" "$LAST_ERR" "exit 0"
    assert_contains "stderr summary carries the session dir" "$LAST_ERR" \
        "${ACPC_HOME}/sessions/"
    # A blocking dispatch now writes two `--` lines: the early session line and
    # the summary. They share the prefix by design (identical segments), so the
    # summary's own one-line rule is checked by excluding the early line.
    SUMMARY_LINES="$(grep '^-- ' <<<"$LAST_ERR" | grep -vc '^-- session ' || true)"
    assert_eq "the summary is exactly one -- line" "1" "$SUMMARY_LINES"
    EARLY_ONCE="$(grep -c '^-- session ' <<<"$LAST_ERR" || true)"
    assert_eq "dispatch adds exactly one more -- line, the early one" "1" "$EARLY_ONCE"

    run_acpc run mock "slow:2 smoke early dispatch"
    assert_eq "blocking run with early session line exits 0" "0" "$LAST_RC"
    assert_contains "early session line names the session" "$LAST_ERR" "-- session "
    EARLY_LINES="$(grep -c '^-- session ' <<<"$LAST_ERR" || true)"
    assert_eq "the early session line is emitted once" "1" "$EARLY_LINES"
    assert_contains "early session line names the session dir" "$LAST_ERR" " | dir ${ACPC_HOME}/sessions/"

    run_acpc run mock "layout check" --quiet --json
    assert_json_valid "run --json envelope is valid JSON" "$LAST_OUT"
    LAYOUT_ID="$(json_field "$LAST_OUT" '.session_id')"
    assert_session_id "session id shape" "$LAYOUT_ID"
    LAYOUT_DIR="${ACPC_HOME}/sessions/${LAYOUT_ID}"
    assert_file "session dir exists" "$LAYOUT_DIR"
    assert_file "meta.json written" "${LAYOUT_DIR}/meta.json"
    assert_file "prompt.md written" "${LAYOUT_DIR}/prompt.md"
    assert_file "answer.md written" "${LAYOUT_DIR}/answer.md"
    assert_file "transcript.ndjson written" "${LAYOUT_DIR}/transcript.ndjson"
    assert_eq "answer field matches answer.md bytes" \
        "$(json_field "$LAST_OUT" '.answer')" "$(cat "${LAYOUT_DIR}/answer.md")"
    UTIL_ID="$LAYOUT_ID" # a plain finished session, reused later

    # --dry-run prints resolution with provenance, runs nothing
    SESSIONS_BEFORE="$(find "${ACPC_HOME}/sessions" -maxdepth 1 -mindepth 1 | wc -l)"
    run_acpc run mock "dry run probe" --dry-run
    assert_eq "--dry-run exits 0" "0" "$LAST_RC"
    assert_contains "--dry-run shows the resolved model" "$LAST_OUT" "mock-sonnet-5"
    SESSIONS_AFTER="$(find "${ACPC_HOME}/sessions" -maxdepth 1 -mindepth 1 | wc -l)"
    assert_eq "--dry-run creates no session" "$SESSIONS_BEFORE" "$SESSIONS_AFTER"

    # Prompt sources: exactly one of arg | - | --prompt-file
    run_acpc run mock
    assert_eq "no prompt source is a usage error" "2" "$LAST_RC"
    run_acpc run mock "arg" --prompt-file /dev/null
    assert_eq "two prompt sources is a usage error" "2" "$LAST_RC"
    # Redirect, not a pipe: a pipeline runs run_acpc in a subshell and its
    # LAST_RC/LAST_OUT never reach this shell.
    printf 'stdin prompt body' >"${SCRATCH}/stdin.txt"
    run_acpc run mock - --quiet <"${SCRATCH}/stdin.txt"
    assert_eq "stdin prompt via - works" "0" "$LAST_RC"
    printf 'echo:file prompt body' >"${SCRATCH}/prompt.txt"
    run_acpc run mock --prompt-file "${SCRATCH}/prompt.txt" --quiet
    assert_eq "--prompt-file works" "0" "$LAST_RC"
    assert_contains "--prompt-file prompt reached the agent" "$LAST_OUT" "file prompt body"

    # -o: file gets the answer, stdout gets a short confirmation
    OUT_FILE="${SCRATCH}/dash_o_answer.md"
    run_acpc run mock "dash o test" -o "$OUT_FILE" --quiet
    assert_eq "-o run exits 0" "0" "$LAST_RC"
    assert_file "-o writes the target file" "$OUT_FILE"
    assert_contains "-o stdout confirmation names the path" "$LAST_OUT" "$OUT_FILE"
    assert_not_contains "-o stdout does not carry the answer text" "$LAST_OUT" "## Answer"

    # --max-output: head kept, UTF-8-safe cut, marker names the answer path
    run_acpc run mock "trigger the huge scenario" --quiet
    HUGE_OUT="${SCRATCH}/huge_stdout.txt"
    printf '%s' "$LAST_OUT" >"$HUGE_OUT"
    HUGE_BYTES=$(stat -c '%s' "$HUGE_OUT")
    assert_true "default --max-output caps stdout near 128KiB" \
        "$(((HUGE_BYTES <= 131072 + 512) ? 0 : 1))"
    assert_contains "truncation marker names the full answer path" "$LAST_OUT" "answer.md"
    if python3 -c "open('${HUGE_OUT}', 'rb').read().decode('utf-8')" 2>"${SCRATCH}/utf8_err"; then
        pass
    else
        fail "truncated stdout must be valid UTF-8 at the cut: $(cat "${SCRATCH}/utf8_err")"
    fi

    run_acpc run mock "trigger the huge scenario" --quiet --json --max-output 2000
    assert_json_valid "--json truncation envelope is valid JSON" "$LAST_OUT"
    assert_eq "--json truncation sets truncated: true" "true" \
        "$(json_field "$LAST_OUT" '.truncated')"

    run_acpc run mock "trigger the huge scenario" --quiet --max-output 0
    assert_true "--max-output 0 disables the cap" "$(((${#LAST_OUT} > 131072) ? 0 : 1))"

    # Exit codes: 1 (refusal), 124 (run --timeout cancels), 130 (SIGINT), 141 (SIGPIPE)
    run_acpc run mock "please fail this on purpose" --quiet
    assert_eq "refusal exits 1" "1" "$LAST_RC"

    set +e
    timeout 20 bash -c 'acpc run mock "slow:30 run-timeout probe" --timeout 2 --quiet' \
        >"${SCRATCH}/run_timeout.out" 2>"${SCRATCH}/run_timeout.err"
    RUN_TIMEOUT_RC=$?
    set -e
    assert_eq "run --timeout exits 124" "124" "$RUN_TIMEOUT_RC"

    set +e
    # exec, so SIGINT lands on acpc itself rather than on a wrapper shell that
    # cannot exec away because `acpc` is a function.
    bash -c 'exec uv run --project "$SCRIPT_DIR" acpc run mock "slow:30 sigint probe" --quiet' \
        >"${SCRATCH}/sigint.out" 2>"${SCRATCH}/sigint.err" &
    SIGINT_PID=$!
    sleep 2
    kill -INT "$SIGINT_PID"
    wait "$SIGINT_PID"
    SIGINT_RC=$?
    set -e
    assert_eq "SIGINT on a sync run exits 130" "130" "$SIGINT_RC"

    set +e
    acpc run mock "trigger the huge scenario" --quiet --max-output 0 2>/dev/null | head -n 1 >/dev/null
    SIGPIPE_RC=${PIPESTATUS[0]}
    set -e
    assert_eq "SIGPIPE on a closed stdout exits 141" "141" "$SIGPIPE_RC"

    # S11: a non-message update separates two answer messages without
    # changing stdout's byte identity with the answer on disk.
    SEPARATOR_OUT="${SCRATCH}/separator.out"
    SEPARATOR_ERR="${SCRATCH}/separator.err"
    set +e
    acpc run mock "separator smoke probe" >"$SEPARATOR_OUT" 2>"$SEPARATOR_ERR"
    SEPARATOR_RC=$?
    set -e
    assert_eq "separator smoke run exits 0" "0" "$SEPARATOR_RC"
    SEPARATOR_TEXT="$(cat "$SEPARATOR_OUT")"
    assert_contains "detectable message boundary keeps markdown separated" "$SEPARATOR_TEXT" $'\n\n## Answer'
    SEPARATOR_ID="$(sed -n 's/^-- session \([^ ]*\).*/\1/p' "$SEPARATOR_ERR" | head -n1)"
    SEPARATOR_DIR="${ACPC_HOME}/sessions/${SEPARATOR_ID}"
    if cmp -s "$SEPARATOR_OUT" "${SEPARATOR_DIR}/answer.md"; then
        pass
    else
        fail "stdout matches answer.md bytes for separated answer messages"
    fi

    # On-disk contract: 0700 dirs / 0600 files, meta parses, transcript header
    while IFS= read -r -d '' d; do
        assert_mode "dir is 0700: $d" "$d" "700"
    done < <(find "${ACPC_HOME}/sessions" -type d -print0)
    while IFS= read -r -d '' f; do
        assert_mode "file is 0600: $f" "$f" "600"
    done < <(find "${ACPC_HOME}/sessions" -type f -print0)
    while IFS= read -r -d '' mp; do
        if python3 -c "import json; json.load(open('${mp}'))" >/dev/null 2>&1; then
            pass
        else
            fail "meta.json parses as JSON: $mp"
        fi
    done < <(find "${ACPC_HOME}/sessions" -name 'meta.json' -print0)
    while IFS= read -r -d '' tp; do
        header="$(head -n1 "$tp")"
        if printf '%s' "$header" | jq -e '.schema == "acpc.transcript/1"' >/dev/null 2>&1; then
            pass
        else
            fail "transcript header names the schema version: $tp"
        fi
        body="$(tail -n +2 "$tp")"
        if [[ -n "$body" ]]; then
            assert_ndjson_valid "transcript is whole-line-only NDJSON: $tp" "$body"
        fi
    done < <(find "${ACPC_HOME}/sessions" -name 'transcript.ndjson' -print0)

    end_section S06-run
fi

poll_slow1

# ==============================================================================
# S07-daemon-bg: --bg, wait, SIGTERM detach, daemon status/stop, concurrency
# ==============================================================================
if begin_section S07-daemon-bg "bg dispatch, wait, detach, daemon plumbing, concurrency"; then
    run_acpc run mock "smoke test bg run" --bg
    assert_eq "bg run exits 0" "0" "$LAST_RC"
    assert_eq "bg dispatch prints no stderr summary" "" "$LAST_ERR"
    assert_not_contains "bg dispatch prints no early session line" "$LAST_ERR" "-- session "
    BG1_ID="$(sed -n '1p' <<<"$LAST_OUT")"
    BG1_DIR="$(sed -n '2p' <<<"$LAST_OUT")"
    assert_session_id "bg id shape" "$BG1_ID"
    assert_eq "bg stdout is exactly id + session dir path" "${BG1_ID}
${BG1_DIR}" "$LAST_OUT"
    assert_eq "bg session dir matches the specced path" "${ACPC_HOME}/sessions/${BG1_ID}" "$BG1_DIR"
    assert_file "meta.json exists at dispatch time" "${BG1_DIR}/meta.json"
    assert_file "prompt.md exists at dispatch time" "${BG1_DIR}/prompt.md"

    wait_for_state "$BG1_ID" "done" 20 || fail "BG1 ($BG1_ID) never reached done within 20s"
    run_acpc wait "$BG1_ID" --quiet
    assert_eq "wait collects the bg run with exit 0" "0" "$LAST_RC"
    assert_contains "wait prints the answer" "$LAST_OUT" "smoke test bg run"
    assert_file "answer.md exists after completion" "${BG1_DIR}/answer.md"

    t2=$(date +%s)
    run_acpc wait "$BG1_ID" --quiet
    t3=$(date +%s)
    assert_eq "wait on an already-finished session exits 0 again" "0" "$LAST_RC"
    assert_true "and returns immediately (<3s)" "$(((t3 - t2) <= 3 ? 0 : 1))"

    # SIGTERM detaches: session survives, client exits 143, id printed to stderr
    set +e
    # exec, as for the SIGINT probe: without it SIGTERM kills the wrapper
    # shell, which exits 143 on its own and acpc never gets to detach.
    bash -c 'exec uv run --project "$SCRIPT_DIR" acpc run mock "slow:30 sigterm probe" --quiet' \
        >"${SCRATCH}/sigterm.out" 2>"${SCRATCH}/sigterm.err" &
    SIGTERM_PID=$!
    sleep 2
    kill -TERM "$SIGTERM_PID"
    wait "$SIGTERM_PID"
    SIGTERM_RC=$?
    set -e
    assert_eq "SIGTERM on a sync run exits 143" "143" "$SIGTERM_RC"
    SIGTERM_ERR="$(cat "${SCRATCH}/sigterm.err")"
    SIGTERM_ID="$(
        sed -nE \
            's/.*still RUNNING: ([abcdefghijkmnpqrstuvwxyz23456789]{4,}).*/\1/p' \
            <<<"$SIGTERM_ERR" |
            head -n1
    )"
    assert_true "SIGTERM prints the session id to stderr on the way out" \
        "$([[ -n "$SIGTERM_ID" ]] && echo 0 || echo 1)"
    assert_contains "SIGTERM detach names wait and stop" "$SIGTERM_ERR" "acpc wait"
    run_acpc status "$SIGTERM_ID" --json
    assert_eq "detached session survives SIGTERM (still running)" "running" \
        "$(json_field "$LAST_OUT" '.state')"
    run_acpc stop "$SIGTERM_ID"

    # daemon status / stop
    run_acpc daemon status
    assert_eq "daemon status exits 0" "0" "$LAST_RC"
    assert_contains "daemon status names the mock target" "$LAST_OUT" "mock"
    # Inline-labeled values, so alignment only: the first line is a target, not a header.
    assert_contains "daemon status opens on a target row, not a header" \
        "$(head -n 1 <<<"$LAST_OUT")" "pid"
    # Slow on purpose: the assertion below is about *active* sessions, and a
    # default mock turn is finished well inside the sleep that follows.
    run_acpc run stopper "slow:30 daemon stop victim" --bg --quiet
    DSTOP_ID="$(head -n1 <<<"$LAST_OUT")"
    sleep 1
    run_acpc daemon stop stopper --force
    assert_eq "daemon stop exits 0" "0" "$LAST_RC"
    run_acpc status "$DSTOP_ID" --json
    assert_eq "daemon stop fails its active sessions, never orphans" "failed" \
        "$(json_field "$LAST_OUT" '.state')"
    RECORDED_REASON="$(jq -r '.state' "${ACPC_HOME}/sessions/${DSTOP_ID}/meta.json")"
    assert_eq "the failure is recorded in meta" "failed" "$RECORDED_REASON"

    run_acpc run stopper "slow:30 daemon stop guard" --bg --quiet
    DSTOP_GUARD_ID="$(head -n1 <<<"$LAST_OUT")"
    sleep 1
    run_acpc daemon stop stopper
    assert_eq "daemon stop refuses its active session" "2" "$LAST_RC"
    assert_contains "daemon stop refusal names the active session" "$LAST_ERR" \
        "1 active session (${DSTOP_GUARD_ID})"
    run_acpc status "$DSTOP_GUARD_ID" --json
    assert_eq "guard leaves the session running" "running" \
        "$(json_field "$LAST_OUT" '.state')"
    run_acpc daemon status
    assert_contains "guard leaves the stopper daemon alive" "$LAST_OUT" "stopper"
    run_acpc daemon stop stopper --force
    assert_eq "forced cleanup of the guarded session exits 0" "0" "$LAST_RC"

    run_acpc daemon status --json
    assert_json_valid "daemon status JSON carries idle age" "$LAST_OUT"
    assert_eq "daemon status JSON has idle_seconds" "true" \
        "$(jq 'all(.daemons[]; has("idle_seconds"))' <<<"$LAST_OUT")"

    # Concurrency: parallel bg dispatches, clean transcripts, no false orphans
    for i in 1 2 3 4 5 6; do
        run_acpc run mock "concurrent smoke task ${i}" --bg --quiet &
    done
    wait
    run_acpc status --all --json
    mapfile -t CONC_IDS < <(json_field "$LAST_OUT" \
        '.sessions[] | select(.prompt_snippet | startswith("concurrent smoke task")) | .session_id')
    assert_true "all 6 concurrent dispatches registered" "$(( ${#CONC_IDS[@]} == 6 ? 0 : 1 ))"
    for id in "${CONC_IDS[@]}"; do
        (acpc status "$id" >/dev/null 2>&1) &
        (acpc log "$id" >/dev/null 2>&1) &
    done
    wait
    for id in "${CONC_IDS[@]}"; do
        wait_for_state "$id" "done" 25 || true
    done
    for id in "${CONC_IDS[@]}"; do
        state="$(session_state "$id")"
        if [[ "$state" == "orphaned" ]]; then
            fail "concurrency: session $id wrongly reported orphaned"
        else
            pass
        fi
        trans="${ACPC_HOME}/sessions/${id}/transcript.ndjson"
        body="$(tail -n +2 "$trans")"
        if [[ -n "$body" ]]; then
            assert_ndjson_valid "concurrency: $id transcript has no interleaved lines" "$body"
        fi
    done

    end_section S07-daemon-bg
fi

poll_slow1

# ==============================================================================
# S08-views: status views, log views, footers, cursors
# ==============================================================================
if begin_section S08-views "status list/detail, log default/--since/--tail/--prose/--wait-new/--follow"; then
    # status list: defaults to running + 5 most recent finished; footer in view
    run_acpc status
    assert_eq "status exits 0" "0" "$LAST_RC"
    assert_eq "status list opens with a column header" "id entry model state runtime idle name prompt" \
        "$(awk 'NR == 1 {$1 = $1; print}' <<<"$LAST_OUT")"
    run_acpc status --all
    assert_eq "status --all exits 0" "0" "$LAST_RC"
    # Asserted via --all: at the S08 gate the S07 sessions don't exist yet, and
    # in the full run the S07 burst pushes S06's sessions out of the recent 5.
    assert_contains "status list shows prompt snippets" "$LAST_OUT" "smoke test sync run"
    run_acpc status "$UTIL_ID"
    assert_contains "status <id> shows the session dir" "$LAST_OUT" \
        "${ACPC_HOME}/sessions/${UTIL_ID}"
    run_acpc status "$UTIL_ID" --all
    assert_eq "status <id> --all is a usage error" "2" "$LAST_RC"
    run_acpc status --json
    assert_json_valid "status --json is valid" "$LAST_OUT"
    run_acpc status "$UTIL_ID" --json
    assert_json_valid "status <id> --json is valid" "$LAST_OUT"
    assert_eq "finished status detail has null idle age" "null" "$(json_field "$LAST_OUT" '.idle_seconds')"
    run_acpc status --all --json
    assert_json_valid "status list JSON carries idle age" "$LAST_OUT"
    assert_eq "finished status list has null idle age" "null" \
        "$(jq -r --arg id "$UTIL_ID" '.sessions[] | select(.session_id == $id) | .idle_seconds' <<<"$LAST_OUT")"
    assert_eq "status list JSON names the resolved model" "mock-sonnet-5" \
        "$(jq -r --arg id "$UTIL_ID" '.sessions[] | select(.session_id == $id) | .model' <<<"$LAST_OUT")"
    run_acpc status "$UTIL_ID" --json
    assert_eq "status detail JSON names the resolved model" "mock-sonnet-5" \
        "$(json_field "$LAST_OUT" '.model')"
    run_acpc status "$UTIL_ID"
    assert_contains "status <id> text names the resolved model" "$LAST_OUT" "model: mock-sonnet-5"
    run_acpc status --all
    assert_contains "status list text names the resolved model" "$LAST_OUT" "mock-sonnet-5"

    # log: default view, footer on stderr, cursor there too
    run_acpc log "$UTIL_ID"
    assert_eq "log exits 0" "0" "$LAST_RC"
    assert_contains "log footer (stderr) carries a cursor" "$LAST_ERR" "cursor:"
    assert_not_contains "log stdout does not carry the footer" "$LAST_OUT" "cursor:"
    assert_contains "finished session footer names the answer path" "$LAST_ERR" "answer"
    assert_contains "finished session footer reports page coverage" "$LAST_ERR" "events "

    run_acpc log "$UTIL_ID" --tail 2
    LOG_TAIL_LINES="$(grep -c . <<<"$LAST_OUT" || true)"
    assert_true "--tail 2 prints at most 2 events" "$((LOG_TAIL_LINES <= 2 ? 0 : 1))"

    run_acpc log "$UTIL_ID" --since 999999
    assert_eq "--since far beyond the end is fine" "0" "$LAST_RC"
    assert_eq "--since beyond the end prints no events" "" "$LAST_OUT"

    run_acpc log "$UTIL_ID" --json
    assert_ndjson_valid "log --json is valid NDJSON with indices" "$LAST_OUT"
    FIRST_I="$(head -n1 <<<"$LAST_OUT" | jq -r '.i')"
    assert_true "log --json events carry their index" "$([[ "$FIRST_I" =~ ^[0-9]+$ ]] && echo 0 || echo 1)"

    run_acpc log "$UTIL_ID" --prose --json
    assert_eq "--prose with --json is a usage error" "2" "$LAST_RC"

    # --prose: clean markdown on stdout, footer on stderr
    run_acpc log "$UTIL_ID" --prose
    assert_eq "log --prose exits 0" "0" "$LAST_RC"
    assert_contains "--prose renders the full agent message" "$LAST_OUT" "## Answer"
    assert_not_contains "--prose has no tool lines" "$LAST_OUT" "tool"
    assert_contains "--prose keeps its footer on stderr" "$LAST_ERR" "cursor:"

    # "Nothing new" has to come from a session that cannot produce anything
    # new: SLOW1 emits every ~2s, so any timeout short enough to keep the suite
    # quick is a coin flip against its cadence. A finished session returns
    # immediately with 124 and the finished footer -- SPEC's `--wait-new` row,
    # the `logs -f` convention; completion is `wait`'s job.
    run_acpc log "$UTIL_ID" --wait-new --timeout 30
    assert_eq "log --wait-new returns 124 at once on a finished session" "124" "$LAST_RC"
    assert_contains "the immediate 124 carries the finished footer" "$LAST_ERR" "done"

    # --follow: the bounded call that replaces a hand-rolled --wait-new loop.
    # A finished session ends the stream at once and that ending is success --
    # the `logs -f` convention -- unlike --wait-new's "nothing new" 124.
    run_acpc log "$UTIL_ID" --follow --timeout 30
    assert_eq "log --follow returns 0 at once on a finished session" "0" "$LAST_RC"
    assert_contains "the follow ending carries the finished footer" "$LAST_ERR" "done"
    assert_contains "the follow footer carries the resume cursor" "$LAST_ERR" "cursor:"

    run_acpc log "$UTIL_ID" -f --tail 0
    assert_eq "-f is a real short flag on log" "0" "$LAST_RC"
    assert_eq "--tail 0 starts the follow at the transcript's end" "" "$LAST_OUT"

    run_acpc log "$UTIL_ID" --follow --since 0 --max-output 1
    assert_eq "an exhausted --max-output ends the follow with exit 4" "4" "$LAST_RC"
    assert_contains "the cut names the transcript on stdout" "$LAST_OUT" "output truncated"
    assert_contains "the cut says how to resume" "$LAST_ERR" "--follow --since"

    run_acpc log "$UTIL_ID" --follow --wait-new
    assert_eq "--follow with --wait-new is a usage error" "2" "$LAST_RC"

    # The timeout ending needs a session that is certainly still running.
    # SLOW1's remaining time depends on how long the assertions above took, so
    # this dispatches its own victim rather than racing a shared one.
    run_acpc run mock "slow:20 follow timeout probe" --bg --quiet
    FOLLOW_ID="$(head -n1 <<<"$LAST_OUT")"
    run_acpc log "$FOLLOW_ID" --follow --tail 0 --timeout 3
    assert_eq "log --follow times out with 124 on a running session" "124" "$LAST_RC"
    assert_contains "the follow timeout says the session continues" "$LAST_ERR" \
        "still running (gave up waiting"
    assert_contains "the follow timeout carries the resume cursor" "$LAST_ERR" "cursor:"
    run_acpc stop "$FOLLOW_ID"
    assert_eq "the follow probe stops cleanly" "0" "$LAST_RC"

    if [[ $SLOW_MACHINERY -eq 1 ]]; then
        # Live long-poll. SLOW1 only lives ~64s, and this section reaches here
        # later than that on a loaded machine, so the poll gets its own victim
        # rather than racing SLOW1's completion.
        run_acpc run mock "run the slow scenario for the wait-new probe" --bg --quiet
        WAITNEW_ID="$(head -n1 <<<"$LAST_OUT")"
        run_acpc log "$WAITNEW_ID" --json
        WAITNEW_CURSOR="$(jq -r '.i' <<<"$LAST_OUT" | tail -n1)"
        run_acpc log "$WAITNEW_ID" --since "${WAITNEW_CURSOR:-0}" --wait-new --timeout 10
        assert_eq "log --wait-new returns once new events arrive" "0" "$LAST_RC"
        assert_true "--wait-new produced output" "$([[ -n "$LAST_OUT" ]] && echo 0 || echo 1)"
        run_acpc stop "$WAITNEW_ID"
        assert_eq "the wait-new probe stops cleanly" "0" "$LAST_RC"
        poll_slow1

        # Snippet vs prose on the long (>200 chars) mid-run message
        run_acpc log "$SLOW1_ID" --since 0
        assert_not_contains "default view truncates the long msg" "$LAST_OUT" \
            "will confirm that before touching any source."
        run_acpc log "$SLOW1_ID" --prose --since 0
        assert_contains "--prose shows the long msg in full" "$LAST_OUT" \
            "will confirm that before touching any source."

        # Let SLOW1 finish; cross-check the poller against the raw transcript.
        wait_for_state "$SLOW1_ID" "done" 90 || fail "SLOW1 never reached done within 90s"
        poll_slow1
        run_acpc log "$SLOW1_ID"
        assert_contains "finished footer includes exit code" "$LAST_ERR" "exit 0"
        assert_contains "finished footer includes the answer path" "$LAST_ERR" "answer"
        RAW_IS="$(tail -n +2 "${ACPC_HOME}/sessions/${SLOW1_ID}/transcript.ndjson" \
            | jq -r '.i' | sort -n)"
        POLLED_IS="$(sort -n "$SLOW1_SEEN")"
        assert_eq "poller saw every SLOW1 event exactly once, none skipped" \
            "$RAW_IS" "$POLLED_IS"
    fi

    # log edge cases
    run_acpc log "$UTIL_ID" --since -5
    assert_eq "--since negative is a usage error" "2" "$LAST_RC"
    run_acpc log "$UTIL_ID" --tail -1
    assert_eq "--tail negative is a usage error" "2" "$LAST_RC"
    run_acpc log "does-not-exist"
    assert_eq "log on an unknown id is a usage error" "2" "$LAST_RC"

    # An explicit cursor past the transcript's end is a stderr note, not an
    # error; the quiet form suppresses the note with the footer.
    run_acpc log "$UTIL_ID" --since 999999
    assert_eq "past-end --since keeps the snapshot successful" "0" "$LAST_RC"
    assert_eq "past-end --since keeps stdout empty" "" "$LAST_OUT"
    assert_contains "past-end --since names the highest cursor" "$LAST_ERR" \
        "-- --since 999999 is past the transcript's end (highest cursor:"
    run_acpc log "$UTIL_ID" --since 999999 --quiet
    assert_eq "quiet past-end --since stays successful" "0" "$LAST_RC"
    assert_eq "quiet past-end --since suppresses stderr" "" "$LAST_ERR"

    end_section S08-views
fi

# ==============================================================================
# S09-continue: context retention, rotation, cross-turn cursor space
# ==============================================================================
if begin_section S09-continue "continue + steer: context, turn rotation, cursor space"; then
    run_acpc run mock "turn one of the conversation" --quiet --json
    CONVO_ID="$(json_field "$LAST_OUT" '.session_id')"

    run_acpc continue "$CONVO_ID" "turn two, please build on turn one" --quiet
    assert_eq "continue exits 0" "0" "$LAST_RC"
    assert_contains "continue's answer references the earlier turn" "$LAST_OUT" \
        "turn one of the conversation"

    assert_file "prompt.1.md kept for the earlier turn" \
        "${ACPC_HOME}/sessions/${CONVO_ID}/prompt.1.md"
    assert_file "answer.1.md kept for the earlier turn" \
        "${ACPC_HOME}/sessions/${CONVO_ID}/answer.1.md"
    assert_file "prompt.md is the latest turn's" "${ACPC_HOME}/sessions/${CONVO_ID}/prompt.md"

    # Cursor space unbroken across turns: 1..N, no gaps or resets
    CONVO_IS="$(tail -n +2 "${ACPC_HOME}/sessions/${CONVO_ID}/transcript.ndjson" | jq -r '.i')"
    CONVO_EXPECTED="$(seq 1 "$(wc -l <<<"$CONVO_IS")")"
    assert_eq "transcript cursor space is contiguous across turns" \
        "$CONVO_EXPECTED" "$CONVO_IS"

    # A run-only flag on continue names the rule, not a bare unrecognized-argument
    run_acpc continue "$CONVO_ID" "third turn" --permissions write
    assert_eq "run-only flag on continue is a usage error" "2" "$LAST_RC"
    assert_contains "the error names the rule" "$LAST_ERR" "--permissions"

    run_acpc continue does-not-exist "hi"
    assert_eq "continue on an unknown id is a usage error" "2" "$LAST_RC"

    # continue by --name alias
    run_acpc run mock "named session turn one" --quiet --name smoke-named --json
    run_acpc continue smoke-named "named session turn two" --quiet
    assert_eq "continue by name works" "0" "$LAST_RC"

    # steer: cancel the turn in flight and redirect the session, one verb.
    run_acpc run mock "chunkslow:30 steer victim" --bg --quiet
    STEER_ID="$(head -n1 <<<"$LAST_OUT")"
    wait_for_state "$STEER_ID" "running" 20 || fail "the steer victim never started running"
    run_acpc steer "$STEER_ID" "stop what you are doing and summarize instead"
    assert_eq "steer exits 0" "0" "$LAST_RC"
    assert_contains "the redirected turn answers the instruction" "$LAST_OUT" \
        "stop what you are doing and summarize instead"
    assert_contains "the stored prompt carries the interruption preamble" \
        "$(cat "${ACPC_HOME}/sessions/${STEER_ID}/prompt.md")" \
        "Your previous turn was interrupted by the operator"
    assert_file "the interrupted turn's answer is parked" \
        "${ACPC_HOME}/sessions/${STEER_ID}/answer.1.md"
    run_acpc steer "$STEER_ID" "and once more"
    assert_eq "steer on a finished session is a usage error" "2" "$LAST_RC"
    assert_contains "the finished-session error names continue" "$LAST_ERR" "acpc continue"

    end_section S09-continue
fi

poll_slow1

# ==============================================================================
# S10-agents: agents list/detail/--models/--commands/--check/init, install
# ==============================================================================
if begin_section S10-agents "agents views, variants, advertised data, install"; then
    run_acpc agents
    assert_eq "agents (list) exits 0" "0" "$LAST_RC"
    assert_contains "agents list shows mock installed" "$LAST_OUT" "installed"
    assert_contains "agents list shows phantom missing with install hint" "$LAST_OUT" \
        "missing → acpc install phantom"
    assert_contains "agents list shows the builder variant" "$LAST_OUT" "builder"
    assert_eq "agents list labels the variant columns, once" \
        "entry model effort permissions home description" \
        "$(awk '/^  entry / {$1 = $1; print; exit}' <<<"$LAST_OUT")"
    assert_contains "agents list shows the explorer variant" "$LAST_OUT" "explorer"
    assert_contains "variant rows show their model delta" "$LAST_OUT" "mock-opus-5"

    run_acpc agents builder
    assert_contains "agents <variant> shows resolved model" "$LAST_OUT" "mock-opus-5"
    assert_contains "agents <variant> shows provenance" "$LAST_OUT" "(entry)"
    assert_contains "variant view points at the parent for catalogs" "$LAST_OUT" "agents mock"

    run_acpc agents mock
    assert_eq "agents <adapter> exits 0" "0" "$LAST_RC"
    assert_contains "adapter view lists modes" "$LAST_OUT" "yolo"
    assert_contains "adapter view ends with a cache-age footer" "$LAST_OUT" "cached"

    run_acpc agents mock --models
    assert_contains "agents <name> --models lists presets" "$LAST_OUT" "fast"
    assert_contains "presets carry model + effort pairs" "$LAST_OUT" "mock-haiku-4-5"
    assert_eq "the preset table carries a column header" "presets tier model effort" \
        "$(awk 'NR == 1 {$1 = $1; print}' <<<"$LAST_OUT")"
    run_acpc agents --models
    assert_contains "agents --models overview lists mock" "$LAST_OUT" "mock"
    assert_contains "agents --models overview collapses variants" "$LAST_OUT" "builder"
    assert_eq "the overview labels its preset columns" "presets tier model effort" \
        "$(awk '/^  presets / {$1 = $1; print; exit}' <<<"$LAST_OUT")"
    assert_eq "the overview labels its variant columns" "variants entry model effort" \
        "$(awk '/^  variants / {$1 = $1; print; exit}' <<<"$LAST_OUT")"

    run_acpc agents mock --commands
    assert_contains "agents mock --commands lists /review" "$LAST_OUT" "/review"
    assert_contains "commands footer names the full-text cache file" "$LAST_OUT" "commands.md"

    run_acpc agents mock --check
    assert_eq "agents mock --check exits 0 (reachable)" "0" "$LAST_RC"
    run_acpc agents phantom --check
    assert_eq "agents phantom --check exits 1 (unreachable)" "1" "$LAST_RC"

    run_acpc agents init smoke-variant --extends mock --model mock-opus-5 --effort xhigh
    assert_eq "agents init exits 0" "0" "$LAST_RC"
    assert_file "agents init scaffolds a toml" "${ACPC_HOME}/agents/smoke-variant.toml"
    run_acpc agents smoke-variant
    assert_contains "the scaffolded variant resolves" "$LAST_OUT" "mock-opus-5"

    run_acpc install mock
    assert_eq "install mock (already installed) exits 0" "0" "$LAST_RC"
    run_acpc install phantom
    assert_eq "install phantom (installer fails) exits 1" "1" "$LAST_RC"
    run_acpc install unknown-agent-xyz
    assert_eq "install of an unknown agent is a usage error" "2" "$LAST_RC"

    run_acpc agents --json
    assert_json_valid "agents --json is valid" "$LAST_OUT"
    run_acpc agents mock --json
    assert_json_valid "agents mock --json is valid" "$LAST_OUT"

    # Malformed entry TOML: clean error, recovers once removed
    echo 'this is not valid toml [[[' >"${ACPC_HOME}/agents/broken.toml"
    run_acpc agents
    assert_eq "a malformed entry file is a clean error" "2" "$LAST_RC"
    assert_not_contains "malformed entry error has no traceback" "$LAST_ERR" "Traceback"
    rm -f "${ACPC_HOME}/agents/broken.toml"
    run_acpc agents
    assert_eq "agents recovers once the malformed entry is removed" "0" "$LAST_RC"

    # Entry descriptions: present values render in every roster/detail view;
    # absent values remain absent in text and become null in JSON.
    run_acpc agents
    assert_contains "agents list appends the variant description" "$LAST_OUT" \
        "Implements a task against a plan."
    run_acpc agents builder
    assert_contains "agents detail renders the variant description" "$LAST_OUT" \
        "description  Implements a task against a plan."
    run_acpc agents mock
    assert_not_contains "agents detail omits an absent description" "$LAST_OUT" \
        "description  "
    run_acpc agents --json
    assert_eq "agents list JSON carries the description" "Implements a task against a plan." \
        "$(jq -r '.agents[] | select(.name == "builder") | .description' <<<"$LAST_OUT")"
    assert_eq "agents list JSON uses null when absent" "null" \
        "$(jq -r '.agents[] | select(.name == "mock") | .description' <<<"$LAST_OUT")"
    run_acpc agents builder --json
    assert_eq "agents detail JSON carries the description" "Implements a task against a plan." \
        "$(jq -r '.description' <<<"$LAST_OUT")"
    run_acpc agents mock --json
    assert_eq "agents detail JSON uses null when absent" "null" \
        "$(jq -r '.description' <<<"$LAST_OUT")"

    cat >"${ACPC_HOME}/agents/long-description.toml" <<'EOF'
extends = "mock"
description = """A deliberately long description with   repeated whitespace
and enough words to exceed the list view budget while preserving its full detail value."""
EOF
    LONG_DESCRIPTION=$'A deliberately long description with   repeated whitespace\nand enough words to exceed the list view budget while preserving its full detail value.'
    run_acpc run long-description "description dispatch" --quiet
    assert_eq "long and multiline description does not break dispatch" "0" "$LAST_RC"
    run_acpc agents
    assert_contains "agents list normalizes and truncates descriptions" "$LAST_OUT" \
        "A deliberately long description with repeated whitespace and enough words to..."
    run_acpc agents long-description
    assert_contains "agents detail keeps the full multiline description" "$LAST_OUT" \
        "$LONG_DESCRIPTION"
    run_acpc agents --json
    assert_eq "agents list JSON keeps the full description" "$LONG_DESCRIPTION" \
        "$(jq -r '.agents[] | select(.name == "long-description") | .description' <<<"$LAST_OUT")"
    run_acpc agents long-description --json
    assert_eq "agents detail JSON keeps the full description" "$LONG_DESCRIPTION" \
        "$(jq -r '.description' <<<"$LAST_OUT")"

    end_section S10-agents
fi

# ==============================================================================
# S11-maintenance: stop semantics, rm, prune
# ==============================================================================
if begin_section S11-maintenance "stop no-op semantics, rm, prune"; then
    run_acpc stop "$UTIL_ID"
    assert_eq "stop on a finished session is a no-op, exit 0" "0" "$LAST_RC"
    run_acpc stop does-not-exist
    assert_eq "stop on an unknown id is a usage error" "2" "$LAST_RC"

    run_acpc run mock "session to be removed" --quiet --json
    RM_ID="$(json_field "$LAST_OUT" '.session_id')"
    run_acpc rm "$RM_ID"
    assert_eq "rm on a finished session exits 0" "0" "$LAST_RC"
    assert_true "rm deletes the session dir" \
        "$([[ ! -e "${ACPC_HOME}/sessions/${RM_ID}" ]] && echo 0 || echo 1)"
    run_acpc rm "$RM_ID"
    assert_eq "rm on an already-gone id is a usage error" "2" "$LAST_RC"

    run_acpc prune --older-than 100d --dry-run
    assert_eq "prune --dry-run exits 0" "0" "$LAST_RC"

    # Real prune, targeted: back-date two dedicated sessions rather than
    # nuking every finished session (age measured from when they finished).
    run_acpc run mock "prune candidate A" --quiet --json
    PRUNE_A_ID="$(json_field "$LAST_OUT" '.session_id')"
    run_acpc run mock "prune candidate B" --quiet --json
    PRUNE_B_ID="$(json_field "$LAST_OUT" '.session_id')"
    for pid in "$PRUNE_A_ID" "$PRUNE_B_ID"; do
        python3 - "$ACPC_HOME" "$pid" <<'PYEOF'
import json
import sys

meta_path = f"{sys.argv[1]}/sessions/{sys.argv[2]}/meta.json"
with open(meta_path) as fh:
    meta = json.load(fh)
for key in ("created_at", "started_at", "finished_at"):
    if isinstance(meta.get(key), (int, float)):
        meta[key] = meta[key] - 200 * 86400
with open(meta_path, "w") as fh:
    json.dump(meta, fh)
PYEOF
    done

    run_acpc prune --older-than 100d --dry-run
    assert_contains "prune --dry-run finds candidate A" "$LAST_OUT" "$PRUNE_A_ID"
    assert_contains "prune --dry-run finds candidate B" "$LAST_OUT" "$PRUNE_B_ID"
    assert_file "dry-run deletes nothing" "${ACPC_HOME}/sessions/${PRUNE_A_ID}"

    run_acpc prune --older-than 100d
    assert_eq "real prune exits 0" "0" "$LAST_RC"
    assert_true "prune removed candidate A" \
        "$([[ ! -e "${ACPC_HOME}/sessions/${PRUNE_A_ID}" ]] && echo 0 || echo 1)"
    assert_true "prune removed candidate B" \
        "$([[ ! -e "${ACPC_HOME}/sessions/${PRUNE_B_ID}" ]] && echo 0 || echo 1)"
    assert_file "an unrelated finished session survives" "${ACPC_HOME}/sessions/${UTIL_ID}"

    end_section S11-maintenance
fi

# ==============================================================================
# S13-permissions: policy tiers visible in log, bypass-mode guard
# ==============================================================================
if begin_section S13-permissions "permission tiers in log, bypass-mode rejection"; then
    # The mock's perm scenario requests: read, edit, execute, delete,
    # switch_mode->yolo (bypass), switch_mode->plan (ordinary).
    run_acpc run mock "run the perm scenario" --permissions read --quiet --json
    PERM_READ_ID="$(json_field "$LAST_OUT" '.session_id')"
    run_acpc log "$PERM_READ_ID" --since 0
    assert_contains "read tier: denials are visible in log" "$LAST_OUT" "denied"
    assert_true "read tier: the edit request was denied" \
        "$(grep -E 'edit' <<<"$LAST_OUT" | grep -qE 'denied' && echo 0 || echo 1)"
    assert_true "read tier: the execute request was denied" \
        "$(grep -E 'execute|Bash' <<<"$LAST_OUT" | grep -qE 'denied' && echo 0 || echo 1)"
    ANSWER_READ="$(cat "${ACPC_HOME}/sessions/${PERM_READ_ID}/answer.md")"
    assert_contains "read tier: read allowed (mock's own summary)" "$ANSWER_READ" \
        "Allowed: read:src/app.py"
    assert_contains "read tier: ordinary switch_mode allowed" "$ANSWER_READ" "switch_mode:plan"
    assert_true "read tier: bypass switch_mode denied" \
        "$(grep -A2 'Denied:' <<<"$ANSWER_READ" | grep -q 'switch_mode:yolo' && echo 0 || echo 1)"

    run_acpc run mock "run the perm scenario" --permissions write --quiet --json
    PERM_WRITE_ID="$(json_field "$LAST_OUT" '.session_id')"
    ANSWER_WRITE="$(cat "${ACPC_HOME}/sessions/${PERM_WRITE_ID}/answer.md")"
    ALLOWED_WRITE="${ANSWER_WRITE%%Denied:*}"
    DENIED_WRITE="${ANSWER_WRITE#*Denied:}"
    assert_contains "write tier: edit allowed" "$ALLOWED_WRITE" "edit:src/app.py"
    assert_contains "write tier: execute allowed" "$ALLOWED_WRITE" "execute:rm -rf build/"
    assert_contains "write tier: delete still denied" "$DENIED_WRITE" "delete:old_report.md"
    assert_contains "write tier: bypass switch_mode still denied" "$DENIED_WRITE" \
        "switch_mode:yolo"

    run_acpc run mock "run the perm scenario" --permissions none --quiet --json
    PERM_NONE_ID="$(json_field "$LAST_OUT" '.session_id')"
    ANSWER_NONE="$(cat "${ACPC_HOME}/sessions/${PERM_NONE_ID}/answer.md")"
    assert_contains "none tier: everything denied" "$ANSWER_NONE" "Allowed: none"

    # Bypass mode at parse time: rejected unless --permissions all
    run_acpc run mock "hi" --mode yolo --dry-run
    assert_eq "bypass mode without --permissions all is a usage error" "2" "$LAST_RC"
    run_acpc run mock "hi" --mode yolo --permissions all --dry-run
    assert_eq "bypass mode with --permissions all is accepted" "0" "$LAST_RC"

    end_section S13-permissions
fi

# ==============================================================================
# S12-cli: help contract, versions, TTY rules, hostile inputs
# ==============================================================================
if begin_section S12-cli "help contract, -V, TTY rules, hostile inputs"; then
    # Two-level help: root cheat sheet <= 100 lines; command pages differ.
    run_acpc --help
    HELP_MAIN="$LAST_OUT"
    HELP_LINES=$(wc -l <<<"$HELP_MAIN")
    assert_true "--help is <= 100 lines" "$((HELP_LINES <= 100 ? 0 : 1))"
    assert_contains "cheat sheet has a write-task example with --permissions write" \
        "$HELP_MAIN" "--permissions write"
    assert_contains "cheat sheet ends with a flag -> ACP mapping" "$HELP_MAIN" "request_permission"
    assert_contains "cheat sheet explains write permissions" "$HELP_MAIN" "write (= edit + execute)"
    for group in "Short task" "Long or uncertain task" "Checking on a run" \
        "Steering a running session" "Context care" "Maintenance and setup" "Common commands"; do
        assert_contains "cheat sheet groups by task: '$group'" "$HELP_MAIN" "$group"
    done
    assert_contains "cheat sheet says wait already prints the answer" \
        "$HELP_MAIN" "block until done, prints the answer"
    assert_contains "cheat sheet frames the file read as the fallback" \
        "$HELP_MAIN" "Truncated or huge answer?"
    assert_contains "cheat sheet warns that killing acpc leaves the session running" \
        "$HELP_MAIN" "acpc stop does."
    assert_contains "cheat sheet frames --follow as the supervision case" \
        "$HELP_MAIN" "case for --follow"
    run_acpc -h
    assert_eq "-h matches --help" "$HELP_MAIN" "$LAST_OUT"

    run_acpc run --help
    assert_true "run --help is its own reference, not the root page" \
        "$([[ "$LAST_OUT" != "$HELP_MAIN" ]] && echo 0 || echo 1)"
    assert_contains "run --help documents --max-output" "$LAST_OUT" "--max-output"
    assert_contains "run --help explains write permissions" "$LAST_OUT" "write (= edit + execute)"
    run_acpc log --help
    assert_contains "log --help documents --wait-new" "$LAST_OUT" "--wait-new"
    assert_contains "log --help documents --follow" "$LAST_OUT" "--follow"
    assert_contains "log --help documents the follow exit codes" "$LAST_OUT" "exit 124"
    run_acpc steer --help
    assert_contains "steer --help documents the interruption" "$LAST_OUT" "Interrupt"
    assert_contains "steer --help documents --prompt-file" "$LAST_OUT" "--prompt-file"
    for verb in stop rm install; do
        run_acpc "$verb" --help
        assert_true "'$verb --help' is its own reference page" \
            "$([[ "$LAST_OUT" != "$HELP_MAIN" ]] && echo 0 || echo 1)"
        assert_contains "'$verb --help' has an example" "$LAST_OUT" "Example"
    done

    for help_flag in -h --help; do
        run_acpc agents "$help_flag"
        assert_eq "agents $help_flag succeeds" "0" "$LAST_RC"
        assert_contains "agents $help_flag documents its options" "$LAST_OUT" "--models"
    done
    run_acpc wait --help
    assert_contains "wait help explains an absent timeout" "$LAST_OUT" "indefinitely"
    assert_contains "wait help shows the max-output default" "$LAST_OUT" "131072"
    run_acpc log --help
    assert_contains "log help explains its last-20 default" "$LAST_OUT" "last 20 events"
    assert_contains "log help explains an absent timeout" "$LAST_OUT" "indefinitely"
    assert_contains "log help shows the max-output default" "$LAST_OUT" "131072"

    run_acpc -V
    assert_contains "-V prints the version" "$LAST_OUT" "acpc"
    V_OUTPUT="$LAST_OUT"
    run_acpc --version
    assert_eq "--version matches -V" "$V_OUTPUT" "$LAST_OUT"

    # TTY vs non-TTY (this script is non-TTY: stdout is captured)
    run_acpc status last
    assert_eq "'last' selector rejected under non-TTY" "2" "$LAST_RC"
    assert_contains "'last' rejection names the reason" "$LAST_ERR" "TTY"
    run_acpc run mock "hi" --permissions prompt --dry-run
    assert_eq "explicit --permissions prompt under non-TTY is a usage error" "2" "$LAST_RC"
    run_acpc run mock "hi" --permissions prompt --bg
    assert_eq "--permissions prompt with --bg is the same usage error" "2" "$LAST_RC"
    run_acpc run mock "hi" --dry-run
    assert_contains "non-TTY default permissions is read (visible in --dry-run)" \
        "$LAST_OUT" "read"

    # setsid + pipe drive the non-TTY path explicitly
    set +e
    setsid bash -c 'acpc status last' </dev/null >"${SCRATCH}/setsid.out" 2>"${SCRATCH}/setsid.err"
    SETSID_RC=$?
    set -e
    assert_eq "'last' rejected under setsid" "2" "$SETSID_RC"

    # TTY-positive checks via a pty, best-effort
    cat >"${SCRATCH}/pty_check.py" <<'PYEOF'
import os
import pty
import select
import subprocess
import sys
import time


def run_in_pty(argv, timeout=15.0):
    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    out = b""
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 0.3)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            elif proc.poll() is not None:
                break
    finally:
        try:
            os.close(master)
        except OSError:
            pass
    rc = proc.wait(timeout=5)
    return rc, out.decode(errors="replace")


def report(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"PTY_ASSERT {name} {status} {detail}".replace("\n", " "))


project = sys.argv[1]
base = ["uv", "run", "--project", project, "acpc"]
try:
    rc, out = run_in_pty(base + ["run", "mock", "pty dry run check", "--dry-run"])
    report("pty_dry_run_exit0", rc == 0, f"rc={rc}")
    report("pty_permissions_default_prompt", "prompt" in out, out[-300:])
except Exception as exc:  # pty is platform-sensitive; skip, don't fail
    print(f"PTY_SKIP {exc!r}")
PYEOF
    set +e
    PTY_OUTPUT="$(python3 "${SCRATCH}/pty_check.py" "$SCRIPT_DIR" 2>"${SCRATCH}/pty_check.err")"
    set -e
    if grep -q '^PTY_SKIP' <<<"$PTY_OUTPUT"; then
        PTY_SKIPPED=1
        progress "pty checks skipped: $(grep '^PTY_SKIP' <<<"$PTY_OUTPUT")"
    else
        while IFS= read -r line; do
            [[ "$line" == PTY_ASSERT* ]] || continue
            name="$(awk '{print $2}' <<<"$line")"
            status="$(awk '{print $3}' <<<"$line")"
            detail="$(cut -d' ' -f4- <<<"$line")"
            if [[ "$status" == "PASS" ]]; then
                pass
            else
                fail "pty: $name / got: [$detail]"
            fi
        done <<<"$PTY_OUTPUT"
    fi

    # Hostile inputs
    NESTED_HOME="${SCRATCH}/nested/does/not/exist/yet"
    ACPC_HOME="$NESTED_HOME" run_acpc status
    assert_eq "a fresh nested ACPC_HOME yields a clean empty list" "0" "$LAST_RC"

    run_acpc run mock "hostile: corrupt meta" --quiet --json
    HOSTILE_ID="$(json_field "$LAST_OUT" '.session_id')"
    HOSTILE_META="${ACPC_HOME}/sessions/${HOSTILE_ID}/meta.json"
    cp "$HOSTILE_META" "${SCRATCH}/meta_backup.json"
    echo '{not valid json' >"$HOSTILE_META"
    run_acpc status "$HOSTILE_ID"
    assert_true "corrupt meta.json is a clean nonzero error" \
        "$([[ "$LAST_RC" != "0" ]] && echo 0 || echo 1)"
    assert_not_contains "corrupt meta.json error has no traceback" "$LAST_ERR" "Traceback"
    cp "${SCRATCH}/meta_backup.json" "$HOSTILE_META"

    printf '{"broken' >>"${ACPC_HOME}/sessions/${HOSTILE_ID}/transcript.ndjson"
    run_acpc log "$HOSTILE_ID"
    assert_eq "a truncated trailing transcript line does not crash log" "0" "$LAST_RC"
    assert_not_contains "truncated transcript line: no traceback" "$LAST_ERR" "Traceback"
    rm -rf "${ACPC_HOME:?}/sessions/${HOSTILE_ID:?}"

    run_acpc run mock "" --quiet
    assert_eq "an empty prompt does not crash run" "0" "$LAST_RC"
    printf 'line one\nline two\x01\x02control chars' >"${SCRATCH}/ctrl_prompt.txt"
    run_acpc run mock --prompt-file "${SCRATCH}/ctrl_prompt.txt" --quiet
    assert_eq "control chars in the prompt do not crash run" "0" "$LAST_RC"

    for verb_args in "run mock hi --bogus" "continue ${UTIL_ID} hi --bogus" "status --bogus" \
        "log ${UTIL_ID} --bogus" "wait ${UTIL_ID} --bogus" "stop ${UTIL_ID} --bogus" \
        "rm ${UTIL_ID} --bogus" "prune --bogus" "agents --bogus" "install mock --bogus" \
        "daemon status --bogus"; do
        # shellcheck disable=SC2086
        run_acpc $verb_args
        assert_eq "unknown flag on '${verb_args}' is a usage error" "2" "$LAST_RC"
        assert_not_contains "unknown flag on '${verb_args}': no traceback" "$LAST_ERR" "Traceback"
    done

    # Neighboring docker/systemctl spellings are hints, never aliases.
    declare -A DAEMON_HINTS=(
        ["daemon list"]="Error: no such command 'list' — the daemon view is: acpc daemon status"
        ["daemon ls"]="Error: no such command 'ls' — the daemon view is: acpc daemon status"
        ["daemon ps"]="Error: no such command 'ps' — the daemon view is: acpc daemon status"
        ["daemon stop --all"]="Error: --all is not a daemon flag — bare acpc daemon stop already addresses every daemon"
        ["daemon start"]="Error: no such command 'start' — daemons start on first use; acpc daemon stop <agent> and the next run is the restart"
        ["daemon restart"]="Error: no such command 'restart' — daemons start on first use; acpc daemon stop <agent> and the next run is the restart"
    )
    for daemon_args in "${!DAEMON_HINTS[@]}"; do
        # shellcheck disable=SC2086
        run_acpc $daemon_args
        assert_eq "'$daemon_args' is a hint, not an alias" "2" "$LAST_RC"
        assert_eq "'$daemon_args' has the pinned hint" "${DAEMON_HINTS[$daemon_args]}" "$LAST_ERR"
        assert_eq "'$daemon_args' is one line" "1" "$(wc -l <<<"$LAST_ERR")"
        assert_not_contains "'$daemon_args' has no traceback" "$LAST_ERR" "Traceback"
    done
    run_acpc list
    assert_eq "top-level list stays outside daemon hints" "2" "$LAST_RC"
    assert_not_contains "top-level list does not mention daemon status" "$LAST_ERR" "daemon status"

    end_section S12-cli
fi

# ==============================================================================
# S16-skills: bundled skill list/detail views and JSON
# ==============================================================================
if begin_section S16-skills "bundled skill list, detail, metadata, and JSON"; then
    run_acpc skills
    assert_eq "skills (list) exits 0" "0" "$LAST_RC"
    assert_eq "skills list has a column header" "name description" \
        "$(awk 'NR == 1 {$1 = $1; print}' <<<"$LAST_OUT")"
    assert_contains "skills list shows provider-bringup" "$LAST_OUT" "provider-bringup"

    run_acpc skills provider-bringup
    assert_eq "skills <name> exits 0" "0" "$LAST_RC"
    assert_contains "skill detail prints the body" "$LAST_OUT" "# Bringing up a provider"
    assert_contains "skill detail prints its labeled directory on stderr" "$LAST_ERR" \
        "-- skill provider-bringup | dir "
    assert_not_contains "skill detail keeps its directory off stdout" "$LAST_OUT" \
        "-- skill provider-bringup | dir "

    run_acpc skills --json
    assert_json_valid "skills list JSON is valid" "$LAST_OUT"
    assert_eq "skills list JSON is an object keyed by skills" "provider-bringup" \
        "$(jq -r '.skills[0].name' <<<"$LAST_OUT")"
    SKILL_DIR="$(jq -r '.skills[0].path' <<<"$LAST_OUT")"
    assert_file "skills list JSON path points at the skill directory" \
        "${SKILL_DIR}/SKILL.md"

    run_acpc skills provider-bringup --json
    assert_json_valid "skills detail JSON is valid" "$LAST_OUT"
    assert_eq "skills detail JSON includes its body" "provider-bringup" \
        "$(jq -r '.name' <<<"$LAST_OUT")"
    assert_contains "skills detail JSON has body text" "$LAST_OUT" "# Bringing up a provider"

    run_acpc skills does-not-exist
    assert_eq "unknown skill exits 2" "2" "$LAST_RC"
    assert_contains "unknown skill points at acpc skills" "$LAST_ERR" "acpc skills"

    for help_flag in -h --help; do
        run_acpc skills "$help_flag"
        assert_eq "skills $help_flag succeeds" "0" "$LAST_RC"
        assert_contains "skills $help_flag documents JSON" "$LAST_OUT" "--json"
    done

    end_section S16-skills
fi

# ==============================================================================
# Summary
# ==============================================================================
TOTAL=$((PASS_COUNT + FAIL_COUNT))
printf '\n== smoke.sh sections ==\n' >&2
for key in S06-run S07-daemon-bg S08-views S09-continue S10-agents S11-maintenance \
    S13-permissions S12-cli S16-skills; do
    printf '  %-16s %s\n' "$key" "${SECTION_RESULT[$key]:-pending}" >&2
done
if [[ $PTY_SKIPPED -eq 1 ]]; then
    printf 'note: pty-based TTY-positive checks were skipped (see above)\n' >&2
fi
printf '== smoke.sh summary: %d/%d passed, %d failed ==\n' "$PASS_COUNT" "$TOTAL" "$FAIL_COUNT" >&2

if [[ $FAIL_COUNT -gt 0 ]]; then
    exit 1
fi
exit 0
