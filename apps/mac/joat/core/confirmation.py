"""Evidence produced by an authenticated, exact-preview confirmation channel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    action_id: str
    accepted: bool
    challenge_id: str
    session_id: str
    payload_digest: bytes
    preview_digest: bytes
    pairing_identity_digest: bytes
    authorization_digest: bytes
    issued_at_us: int
    expires_at_us: int
    preview_completed_at_us: int
    decided_at_us: int
