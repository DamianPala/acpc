"""Daemon target keying.

SPEC.md `daemon` + *Agent variants*: a daemon is keyed per target — entry name
+ vendor home + declared env. `[env]` contributes by name and value;
`env_passthrough` by name *and the value read at call time* — hashed into the
key, never stored — so two callers holding different credentials are separate
targets that cannot serve each other's traffic. Capability variables (proxies,
ssh agent) are delivered but deliberately not part of the key.

Derived from the 0.3.0.dev1 `target_for_environment`, re-cut for the 0.3 entry
model (no env templates, `home` as a first-class field): the readable
prefix + short-digest scheme and the path-safety guarantees are the donor's.
"""

import hashlib
import json
from collections.abc import Mapping
from urllib.parse import quote

_DIGEST_LENGTH = 16
_MAX_TARGET_BYTES = 180
_UNSET = object()
_PERMISSION_VALUES = ("none", "read", "edit", "execute", "all", "ask")


def target_for_call(
    entry: str,
    *,
    home: str | None = None,
    declared_env: Mapping[str, str] | None = None,
    passthrough_values: Mapping[str, str] | None = None,
    permissions: str | None | object = _UNSET,
    spawn_identity: Mapping[str, str] | None = None,
) -> str:
    """Build a readable, path-safe, stable daemon target for one resolved call.

    `entry` is the agent or variant name the call resolved to. `home` is the
    resolved vendor home (entry field or `--home`). `declared_env` is the
    entry's literal `[env]` table; `passthrough_values` maps each declared
    `env_passthrough` name present in the caller's environment to the value
    read at call time. Secret values shape the digest only — the returned
    target never contains them. The resolved permissions join the key because
    the adapter environment is fixed at spawn. `spawn_identity` captures
    process-level spawn flags that cannot change without a new process (e.g.
    CLI effort on adapters that set effort only at argv time). A completely
    bare entry may omit permissions for the readable-entry optimization;
    every other call must provide a canonical policy.
    """
    safe_entry = quote(entry, safe="-._~")
    declared_env = dict(declared_env or {})
    passthrough_values = dict(passthrough_values or {})
    spawn_identity = dict(spawn_identity or {})
    if permissions is _UNSET:
        if home is None and not declared_env and not passthrough_values and not spawn_identity:
            return safe_entry
        raise ValueError("permissions is required when target inputs need a digest")
    if not isinstance(permissions, str) or permissions not in _PERMISSION_VALUES:
        supported = ", ".join(_PERMISSION_VALUES)
        raise ValueError(f"permissions must be one of: {supported}")
    digest_payload = {
        "entry": entry,
        "home": home,
        "env": dict(sorted(declared_env.items())),
        "passthrough": dict(sorted(passthrough_values.items())),
        "permissions": permissions,
        "spawn": dict(sorted(spawn_identity.items())),
    }
    digest_input = json.dumps(
        digest_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]

    suffix = f"~{digest}"
    max_prefix_bytes = _MAX_TARGET_BYTES - len(suffix)
    prefix = safe_entry.encode("utf-8")[:max_prefix_bytes].decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}"
