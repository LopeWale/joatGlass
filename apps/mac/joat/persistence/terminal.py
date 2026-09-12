"""Atomic, endpoint-bound terminal checkpoints in the engine's leased store."""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlsplit

from ..core.terminal_conversation import TerminalConversation
from .store import DurableStore, StorageError

MAX_SNAPSHOT_BYTES = 2097152


class TerminalCheckpointError(StorageError):
    """Persistence failed; do not submit another remote instruction."""


class TerminalCheckpointStore:
    def __init__(self, store: DurableStore) -> None:
        self._store = store

    @staticmethod
    def _binding(profile_id: str, endpoint: str) -> None:
        parsed = urlsplit(endpoint)
        if (not profile_id or len(profile_id) > 32 or not endpoint or len(endpoint) > 2048
                or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise TerminalCheckpointError("Invalid terminal checkpoint binding")

    def load(self, profile_id: str, endpoint: str) -> dict[str, Any] | None:
        self._binding(profile_id, endpoint)
        row = self._store.execute(
            "SELECT snapshot FROM terminal_checkpoints WHERE profile_id=? AND endpoint=?",
            (profile_id, endpoint),
        ).fetchone()
        if row is None:
            return None
        try:
            raw = bytes(row[0])
            if len(raw) > MAX_SNAPSHOT_BYTES:
                raise ValueError("size")
            value = json.loads(raw)
            if (not isinstance(value, dict) or set(value) != {"version", "profile_id", "endpoint", "provider", "receipt_user_id", "receipt_dispatch_id", "conversation", "adapter"}
                    or value["version"] != 1 or value["profile_id"] != profile_id or value["endpoint"] != endpoint
                    or not isinstance(value["adapter"], dict)):
                raise ValueError("shape")
            TerminalConversation.restore(value["conversation"])
            return value
        except (ValueError, TypeError, UnicodeError, OverflowError):
            raise TerminalCheckpointError("Terminal checkpoint is invalid; no instruction was submitted") from None

    def save(self, profile_id: str, endpoint: str, conversation: TerminalConversation,
             adapter: dict[str, Any], *, provider: str = "generic",
             receipt_user_id: str | None = None, receipt_dispatch_id: str | None = None) -> None:
        self._binding(profile_id, endpoint)
        # Only the adapter's explicit export contract may enter this store.
        # Recursively reject credential-bearing fields even if a future caller
        # accidentally passes connection configuration instead of runtime state.
        def reject_secrets(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if not isinstance(key, str) or any(word in key.casefold() for word in ("token", "secret", "password", "credential")):
                        raise TerminalCheckpointError("Credentials cannot enter terminal checkpoints")
                    reject_secrets(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    reject_secrets(child)
        reject_secrets(adapter)
        payload = {"version": 1, "profile_id": profile_id, "endpoint": endpoint,
                   "provider": provider, "receipt_user_id": receipt_user_id, "receipt_dispatch_id": receipt_dispatch_id,
                   "conversation": conversation.snapshot(), "adapter": adapter}
        try:
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(raw) > MAX_SNAPSHOT_BYTES:
                raise TerminalCheckpointError("Terminal checkpoint exceeds its storage limit")
            with self._store.transaction() as db:
                db.execute(
                    "INSERT INTO terminal_checkpoints(profile_id,endpoint,schema_version,snapshot,revision,updated_at_us) "
                    "VALUES(?,?,1,?,1,?) ON CONFLICT(profile_id,endpoint) DO UPDATE SET "
                    "snapshot=excluded.snapshot,revision=terminal_checkpoints.revision+1,updated_at_us=excluded.updated_at_us",
                    (profile_id, endpoint, raw, time.time_ns() // 1000),
                )
        except TerminalCheckpointError:
            raise
        except Exception:
            raise TerminalCheckpointError("Terminal checkpoint could not be saved; no new instruction may be submitted") from None
