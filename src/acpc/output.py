"""Output formatters for acpc.

Manages the stdout/stderr contract:
- stdout: agent output (text stream, NDJSON, or final-only)
- stderr: diagnostics, always prefixed with [acpc]
"""

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Stderr helpers
# ---------------------------------------------------------------------------


def stderr(msg: str) -> None:
    """Print [acpc]-prefixed message to stderr."""
    print(f"[acpc] {msg}", file=sys.stderr, flush=True)


def stderr_session(session_id: str) -> None:
    """Print session ID to stderr."""
    stderr(f"session: {session_id}")


def stderr_resume(agent: str, session_id: str) -> None:
    """Print resume command hint to stderr."""
    stderr(f'resume: acpc prompt {agent} -s {session_id} "follow up"')


def stderr_tool(title: str) -> None:
    """Print tool call info to stderr."""
    stderr(f"tool: {title}")


def stderr_permission(kind: str, title: str, outcome: str) -> None:
    """Print permission decision to stderr."""
    stderr(f"permission: {kind} {title} -> {outcome}")


def stderr_error(msg: str) -> None:
    """Print error to stderr."""
    stderr(f"error: {msg}")


# ---------------------------------------------------------------------------
# Output mode
# ---------------------------------------------------------------------------


class OutputMode(Enum):
    TEXT = "text"
    JSON = "json"
    QUIET = "quiet"


# ---------------------------------------------------------------------------
# OutputHandler
# ---------------------------------------------------------------------------


class OutputHandler:
    """Manages output based on the selected mode.

    - TEXT: stream agent_message_chunk text to stdout immediately
    - JSON: emit every ACP event as an NDJSON line to stdout
    - QUIET: collect text, emit only the final result
    """

    def __init__(
        self,
        mode: OutputMode,
        output_file: str | None = None,
    ) -> None:
        self.mode = mode
        self.output_file = output_file
        self._chunks: list[str] = []
        self._success = False

    # -- agent text ---------------------------------------------------------

    def on_agent_message_chunk(self, text: str) -> None:
        """Handle agent message text chunk."""
        if self.mode is OutputMode.TEXT:
            sys.stdout.write(text)
            sys.stdout.flush()
        elif self.mode is OutputMode.QUIET:
            self._chunks.append(text)
        # JSON mode: text is part of the full event, handled by on_event

    # -- raw ACP events (JSON mode) ----------------------------------------

    def on_event(self, event: dict[str, Any]) -> None:
        """Write a single ACP session_update event as NDJSON to stdout."""
        if self.mode is OutputMode.JSON:
            print(json.dumps(event, separators=(",", ":")), flush=True)

    # -- tool calls ---------------------------------------------------------

    def on_tool_call(self, title: str, kind: str | None = None) -> None:
        """Handle tool call notification (stderr in TEXT/QUIET modes)."""
        if self.mode in (OutputMode.TEXT, OutputMode.QUIET):
            label = f"{kind}:{title}" if kind else title
            stderr_tool(label)

    # -- session lifecycle --------------------------------------------------

    def on_session_started(self, session_id: str) -> None:
        """Emit session_started meta-event (JSON mode only)."""
        if self.mode is OutputMode.JSON:
            self.on_event({"acpc": "session_started", "session_id": session_id})

    def on_session_ended(
        self,
        session_id: str,
        stop_reason: str,
        exit_code: int,
    ) -> None:
        """Mark success and emit session_ended meta-event (JSON mode)."""
        self._success = True
        if self.mode is OutputMode.JSON:
            self.on_event(
                {
                    "acpc": "session_ended",
                    "session_id": session_id,
                    "stop_reason": stop_reason,
                    "exit_code": exit_code,
                }
            )

    def on_session_error(self, session_id: str, error: str) -> None:
        """Emit session_error meta-event (JSON mode). Do NOT mark success."""
        if self.mode is OutputMode.JSON:
            self.on_event(
                {
                    "acpc": "session_error",
                    "session_id": session_id,
                    "error": error,
                }
            )

    # -- finalize -----------------------------------------------------------

    def finalize(self) -> None:
        """Flush remaining output.

        QUIET mode: write collected text to stdout.
        All modes: write to output_file if session ended successfully.
        """
        if self.mode is OutputMode.QUIET and self._chunks:
            sys.stdout.write("".join(self._chunks))
            sys.stdout.flush()

        if self._success and self.output_file:
            Path(self.output_file).write_text(
                "".join(self._chunks) if self._chunks else "",
                encoding="utf-8",
            )
