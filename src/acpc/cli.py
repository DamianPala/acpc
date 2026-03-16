"""CLI entry point for acpc."""

import click

from acpc import __version__


class RawEpilogGroup(click.Group):
    """Click group that preserves epilog whitespace."""

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:  # noqa: ARG002
        if self.epilog:
            formatter.write("\n")
            # Write each line without rewrapping
            for line in self.epilog.split("\n"):
                formatter.write(f"{line}\n")


CHEAT_SHEET = """\

# acpc cheat sheet

## Quick start
acpc prompt codex "fix the tests"
acpc prompt claude "analyze repo" --model sonnet
echo "prompt" | acpc prompt codex -

## Multi-turn
acpc prompt codex "remember: X=42"
acpc prompt codex --last "what is X?"
acpc prompt codex -s SESSION_ID "follow up"

## Model & mode
acpc prompt codex "task" --model o3           # ACP: session/set_model
acpc prompt claude "plan" --mode plan         # ACP: session/set_mode

## Permissions (default: auto-detect TTY)
acpc prompt codex "task" --permissions all    # approve everything
acpc prompt codex "task" --permissions read   # read-only
acpc prompt codex "task" --permissions write  # read + write, no delete
acpc prompt codex "task" --permissions none   # deny everything (dry run)

## Output (stdout = response, stderr = diagnostics)
acpc prompt codex "task" --quiet              # final text only
acpc prompt codex "task" --json               # NDJSON ACP events
acpc prompt codex "task" -o result.md         # write to file

## Input
acpc prompt codex --input-file prompt.md      # from file
echo "fix" | acpc prompt codex -              # from stdin

## Process management
acpc status                                   # running sessions
acpc stop codex                               # stop by agent
acpc stop -s SESSION_ID                       # stop by session

## Other
acpc agents                                   # list + install status
acpc sessions codex                           # agent sessions (ACP)
acpc install codex                            # install adapter

## Flag -> ACP mapping
# -s        -> session/load          --model   -> session/set_model
# --mode    -> session/set_mode      --cwd     -> session/new (cwd)
# --permissions -> request_permission  Ctrl+C  -> session/cancel
"""


@click.group(cls=RawEpilogGroup, epilog=CHEAT_SHEET)
@click.version_option(__version__, prog_name="acpc")
def cli() -> None:
    """acpc - Thin CLI client for the Agent Client Protocol (ACP)."""
