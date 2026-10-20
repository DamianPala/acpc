"""acpc: thin CLI client for the Agent Client Protocol."""

from typing import Any


def __getattr__(name: str) -> Any:
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib.metadata import version  # import floor

    value = version("acpc")
    globals()[name] = value
    return value
