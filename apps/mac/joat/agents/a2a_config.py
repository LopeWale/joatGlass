"""Non-secret A2A profiles and the endpoint-bound native credential lookup."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .openclaw_native import NativeBridgeAccess


DEFAULT_A2A_CONFIG = Path.home() / "Library/Application Support/JOAT/a2a.json"


@dataclass(frozen=True, slots=True)
class A2AProfile:
    id: str
    label: str
    endpoint: str
    enabled: bool


def _parse_profile(value: object) -> A2AProfile:
    if not isinstance(value, dict) or set(value) != {"id", "label", "endpoint", "enabled"}:
        raise ValueError("A2A profiles must contain only connection settings, never credentials")
    identity, label, endpoint, enabled = (value[k] for k in ("id", "label", "endpoint", "enabled"))
    if not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", identity):
        raise ValueError("A2A connection identity is invalid")
    if not isinstance(label, str) or not 1 <= len(label) <= 32 or label != label.strip() or any(ord(c) < 32 for c in label):
        raise ValueError("A2A connection name is invalid")
    if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2048 or any(c.isspace() or ord(c) < 32 for c in endpoint):
        raise ValueError("A2A endpoint is invalid")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise ValueError("A2A endpoint is invalid") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or "?" in endpoint or "#" in endpoint or "\\" in endpoint
            or (port is not None and port < 1)):
        raise ValueError("Use an HTTPS A2A endpoint without credentials, query, or fragment")
    if type(enabled) is not bool:
        raise ValueError("A2A enabled must be a boolean")
    return A2AProfile(identity, label, endpoint, enabled)


def load_a2a_profiles(path: Path = DEFAULT_A2A_CONFIG) -> list[A2AProfile]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    except OSError:
        raise ValueError("A2A configuration could not be opened safely") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > 65536 or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("A2A configuration must be a bounded, locally owned regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            descriptor = -1
            value = json.loads(source.read(65537))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("A2A configuration is unreadable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict) or set(value) != {"profiles"}:
        raise ValueError("A2A configuration must contain profiles")
    entries = value["profiles"]
    if not isinstance(entries, list) or len(entries) > 16:
        raise ValueError("Configure at most sixteen A2A connections")
    profiles = [_parse_profile(row) for row in entries]
    if len({p.id for p in profiles}) != len(profiles) or len({p.endpoint for p in profiles}) != len(profiles):
        raise ValueError("A2A connection identities and endpoints must be distinct")
    if len({p.label.casefold() for p in profiles}) != len(profiles):
        raise ValueError("A2A connection names must be distinct")
    return profiles


def native_token_loader(
    profile: A2AProfile, bridge: NativeBridgeAccess | None,
) -> Callable[[], Awaitable[str | None]]:
    async def load() -> str | None:
        if bridge is None:
            return None
        response = await bridge.call("a2a.token.load", {"profile_id": profile.id, "endpoint": profile.endpoint})
        result = response.result
        if result is None:
            return None
        if not isinstance(result, dict) or not isinstance(result.get("token"), str):
            raise RuntimeError("The Mac returned an invalid A2A credential response")
        token: str = result["token"]
        if not token or len(token) > 8192 or any(c.isspace() or ord(c) < 32 for c in token):
            raise RuntimeError("The saved A2A credential is invalid")
        return token
    return load
