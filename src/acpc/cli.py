"""CLI entry point.

Stage 1 wires only the executable skeleton: `acpc -V` / `--version` and a
placeholder root. The command surface (run/continue/status/log/wait/stop/rm/
prune/agents/install/daemon) and the two-level `--help` contract land in
Stage 2 per PLAN.md; this module is theirs to build out.
"""

import click

from acpc import __version__


@click.group(invoke_without_command=True)
@click.version_option(__version__, "-V", "--version", message="acpc %(version)s")
@click.help_option("-h", "--help")
@click.pass_context
def main(ctx: click.Context) -> None:
    """acpc — dispatch coding agents over ACP."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
