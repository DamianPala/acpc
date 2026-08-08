"""Environment construction for adapter and daemon processes.

SPEC.md *Agent variants*: the adapter's environment is constructed, not
inherited. Three layers reach it — a base system set (via the acp library's
`default_environment`), capability variables passed through from the caller
(ssh agent, proxies, CA bundles), and the entry's declared env on top. The
rest of the ambient environment never reaches the adapter.

`ACPC_*` variables (minus internal daemon plumbing) are also forwarded so a
spawned daemon or adapter resolves the same state root as its caller —
`ACPC_HOME` is the one that matters; the wildcard keeps test toggles working.
"""

import os
from collections.abc import Mapping, Sequence

from acp.transports import default_environment

from acpc.paths import acpc_home

_PINNED_TERM = "dumb"

# Name of the env var carrying the daemon's spawn payload; never forwarded.
DAEMON_ENV_PAYLOAD = "ACPC_DAEMON_ENV_PAYLOAD"
_INTERNAL_ENV_NAMES = frozenset({DAEMON_ENV_PAYLOAD, "ACPC_PARENT_SESSION"})

_CAPABILITY_BASE = (
    "SSH_AUTH_SOCK",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)
_CAPABILITY_NAMES = frozenset(
    spelling for name in _CAPABILITY_BASE for spelling in (name, name.lower())
)


def passthrough_names(env_passthrough: Sequence[str]) -> tuple[str, ...]:
    """Return the declared passthrough names, deduplicated, order preserved."""
    return tuple(dict.fromkeys(env_passthrough))


def base_environment(
    ambient: Mapping[str, str] | None = None,
    *,
    ceiling: str | None = None,
) -> dict[str, str]:
    """Build the allowlisted environment inherited by an adapter or daemon."""
    ambient = os.environ if ambient is None else ambient
    environment = dict(default_environment())
    for name, value in ambient.items():
        if (
            name.startswith("ACPC_") and name not in _INTERNAL_ENV_NAMES
        ) or name in _CAPABILITY_NAMES:
            environment[name] = value
    environment["TERM"] = _PINNED_TERM
    if ceiling is not None:
        environment["ACPC_CEILING"] = ceiling
    return environment


def environment_overrides(
    declared_environment: Mapping[str, str],
    env_passthrough: Sequence[str],
    ambient: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return declared values plus ambient passthrough values not overridden by them."""
    ambient = os.environ if ambient is None else ambient
    overrides = dict(declared_environment)
    declared_names = set(declared_environment)
    for name in env_passthrough:
        if name not in declared_names and name in ambient:
            overrides[name] = ambient[name]
    return overrides


def adapter_environment(
    declared_environment: Mapping[str, str],
    env_passthrough: Sequence[str] = (),
    ambient: Mapping[str, str] | None = None,
    *,
    ceiling: str | None = None,
) -> dict[str, str]:
    """Build the complete environment delivered to one adapter process."""
    environment = base_environment(ambient, ceiling=ceiling)
    environment["ACPC_HOME"] = str(acpc_home().resolve())
    environment.update(
        environment_overrides(declared_environment, env_passthrough, ambient=ambient)
    )
    # This is an acpc resolved call fact, not an adapter-configurable value.
    # Remove an inherited value when this caller has no resolved ceiling, so a
    # stale nested ceiling cannot escape into a fresh adapter.
    if ceiling is None:
        environment.pop("ACPC_CEILING", None)
    else:
        environment["ACPC_CEILING"] = ceiling
    environment.pop("ACPC_PARENT_SESSION", None)
    return environment
