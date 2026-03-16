"""Shell completion helper for acpc.

Adds a `generate-completion` subcommand that prints or installs
shell completion scripts for bash, zsh, and fish.

Usage:
    from acpc._completion import add_completion_command
    add_completion_command(cli, "acpc")
"""

import os
import platform
import re
import subprocess
from pathlib import Path

import click

APP_NAME = "acpc"


def _detect_shell() -> str:
    shell = os.environ.get("SHELL", "")
    name = Path(shell).name if shell else ""
    if name in ("bash", "zsh", "fish"):
        return name
    return "bash"


def _completion_path(shell: str, app_name: str) -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    is_mac = platform.system() == "Darwin"

    if shell == "bash":
        return data / "bash-completion" / "completions" / app_name
    if shell == "zsh":
        if is_mac and Path("/usr/local/share/zsh/site-functions").is_dir():
            return Path(f"/usr/local/share/zsh/site-functions/_{app_name}")
        return data / "zsh" / "site-functions" / f"_{app_name}"
    # fish
    return config / "fish" / "completions" / f"{app_name}.fish"


def _generate_script(shell: str, app_name: str) -> str:
    env_var = "_{}_COMPLETE".format(re.sub(r"\W", "_", app_name.upper()))
    result = subprocess.run(
        [app_name],
        capture_output=True,
        text=True,
        env={**os.environ, env_var: f"{shell}_source"},
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise click.ClickException(f"Failed to generate {shell} completion for '{app_name}'.")
    return result.stdout


def add_completion_command(cli: click.Group, app_name: str) -> None:
    """Add a `generate-completion` subcommand to a Click group."""

    @cli.command("generate-completion")
    @click.argument(
        "shell",
        required=False,
        type=click.Choice(["bash", "zsh", "fish"]),
    )
    @click.option(
        "-i",
        "--install",
        is_flag=True,
        help="Install completion to the standard OS path.",
    )
    def generate_completion(shell: str | None, install: bool) -> None:
        """Generate or install shell completions."""
        if platform.system() == "Windows":
            click.echo(
                "Shell completions are not supported on Windows.",
                err=True,
            )
            raise SystemExit(1)
        shell = shell or _detect_shell()
        script = _generate_script(shell, app_name)
        if not install:
            click.echo(script, nl=False)
            return
        dest = _completion_path(shell, app_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(script)
        click.echo(f"Installed {shell} completion to {dest}")
