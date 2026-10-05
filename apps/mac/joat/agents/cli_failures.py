"""Read a failed CLI turn for the one thing the wearer can act on.

Both subscription CLIs report trouble as prose on stdout or stderr, and the
phrasing moves between releases. These phrase lists are the observed and
documented wordings, kept in one place so the assistant seats and the
Brainstorm seats classify a failure identically:

- ``claude -p`` with a dead login, 2026-10-04 (Claude Code 2.1.287):
  ``Failed to authenticate: OAuth session expired and could not be
  refreshed`` — the phrase the old classifier missed, so the wearer saw a
  generic "that turn failed" for a day while the real fix was a sign-in.
- Claude Code docs, "Renew an expiring login": ``Login expired · Please run
  /login``.
- ``claude -p`` out of usage: ``You're out of usage credits`` (observed).
"""

from __future__ import annotations

from typing import Literal

FailureKind = Literal["signed_out", "limit", "other"]

_SIGNED_OUT_PHRASES: tuple[str, ...] = (
    "not logged in",
    "please run /login",
    "run /login",
    "login expired",
    "log in again",
    "sign in",
    "failed to authenticate",
    "oauth session expired",
    "could not be refreshed",
    "authentication_error",
)
# Deliberately absent: "unauthorized", "token expired", "refresh token".
# A Codex turn prints the OAuth failures of the wearer's own MCP servers
# (Cloudflare, RunPod — observed 2026-09-04), and reading those as the
# seat's login would mark a working subscription signed out.

_LIMIT_PHRASES: tuple[str, ...] = (
    "usage limit",
    "out of usage",
    "rate limit",
    "usage credits",
    "quota",
    "limit reached",
    "hit your limit",
)


def classify_cli_failure(output: str) -> FailureKind:
    """What a failed turn's captured output says went wrong.

    Signed-out wins over limit: an expired login can surface alongside a
    generic "try again later", and the sign-in is the step that unblocks.
    """
    lowered = output.lower()
    if any(phrase in lowered for phrase in _SIGNED_OUT_PHRASES):
        return "signed_out"
    if any(phrase in lowered for phrase in _LIMIT_PHRASES):
        return "limit"
    return "other"


__all__ = ["FailureKind", "classify_cli_failure"]
