"""State-root layout and filesystem primitives.

SPEC.md *State on disk*: everything lives under `~/.acpc/`, overridable with
`ACPC_HOME` — the only environment variable that configures acpc itself. The
environment is resolved on every call so separate processes (daemon, tests)
can use isolated roots without module reloads.

Dirs are 0700 and files 0600: prompts and transcripts routinely carry
sensitive material. `atomic_write` guarantees no torn reads: same-directory
temp file (mkstemp → 0600) plus `os.replace`.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

PRIVATE_DIR_MODE = 0o700

_DEFAULT_HOME = "~/.acpc"


def acpc_home() -> Path:
    """Return the state root, honoring the `ACPC_HOME` override."""
    configured = os.environ.get("ACPC_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path(_DEFAULT_HOME).expanduser()


def config_file() -> Path:
    return acpc_home() / "config.toml"


def agents_dir() -> Path:
    """User agent entries: variants, adapter overrides, new adapters."""
    return acpc_home() / "agents"


def cache_dir() -> Path:
    """Advertised models, modes, and commands, one subdir per adapter."""
    return acpc_home() / "cache"


def daemon_dir() -> Path:
    """Daemon runtime state: per-target logs, sockets, and lock files."""
    return acpc_home() / "daemon"


def sessions_dir() -> Path:
    return acpc_home() / "sessions"


def session_dir(session_id: str) -> Path:
    return sessions_dir() / session_id


def ensure_private_dir(path: Path) -> Path:
    """Create a directory owner-only, tightening parents up to the state root.

    Only directories under the state root are chmodded; a caller pointing at
    an unrelated path gets a plain private mkdir for the leaf alone.
    """
    root = acpc_home()
    path.mkdir(parents=True, exist_ok=True)
    candidates = [path, *path.parents]
    for directory in candidates:
        if not directory.is_relative_to(root):
            break
        directory.chmod(PRIVATE_DIR_MODE)
    return path


def atomic_write(path: Path, data: dict[str, Any] | str, *, exclusive: bool = False) -> None:
    """Write JSON or text through a same-directory temporary file.

    `exclusive=True` publishes with `os.link`, which fails with `FileExistsError`
    if the path already exists — the create-once primitive used for lock-ish
    markers. The default `os.replace` is the atomic-update primitive.
    mkstemp creates the temp file 0600, so the published file is owner-only.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            if isinstance(data, str):
                file.write(data)
            else:
                json.dump(data, file)
        if exclusive:
            os.link(tmp, path)
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        else:
            os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
