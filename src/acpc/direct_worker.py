"""Private process entry point for a direct turn with a client wait deadline."""

from __future__ import annotations

import os
import sys
from dataclasses import replace

from acpc import interaction
from acpc.runner import _direct_worker_request, run_direct_worker


def _permission_prompt(kind: str, title: str) -> bool:
    return interaction.ask_yes_no(f"acpc: allow {kind}? {title} [y/N] ", default=False)


def main() -> int:
    """Run one private worker without inheriting the caller's output pipes."""
    if len(sys.argv) != 2:
        return 2
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        for file_descriptor in (0, 1, 2):
            os.dup2(devnull, file_descriptor)
    finally:
        os.close(devnull)
    try:
        # The original caller's callback cannot cross the JSON hand-off, but
        # an interactive direct turn can still ask through the controlling tty.
        request = _direct_worker_request(sys.argv[1])
        if request.resolution.permissions == "ask":
            request = replace(request, permission_prompt=_permission_prompt)
        run_direct_worker(sys.argv[1], request)
    except BaseException:  # noqa: BLE001
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
