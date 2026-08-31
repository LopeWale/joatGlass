"""Opt-in Claude task completion via the official CLI (RULES.md #9).

QuickNote summaries default to the local MLX engine so a private meeting
transcript never leaves the Mac. This completer exists only for the explicit
"with claude" ask — the wearer chooses, per note, to trade privacy for a
stronger summary — and it invokes nothing but the official ``claude`` binary
in print mode, exactly like the Brainstorm critic seat.
"""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 240.0


class ClaudeCompleter:
    """One-shot ``claude -p`` completion matching the TaskCompleter shape."""

    def __init__(self, binary: str, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        if not binary:
            raise ValueError("claude binary path must be non-empty")
        if not 1.0 <= timeout_s <= 3_600.0:
            raise ValueError("timeout must be between 1 and 3600 seconds")
        self._binary = binary
        self._timeout_s = timeout_s

    async def complete(
        self, *, system: str, text: str, max_tokens: int = 700
    ) -> str | None:
        # The CLI owns its own generation budget; max_tokens is part of the
        # shared completer shape and deliberately unused here.
        del max_tokens
        if not text.strip() or not system.strip():
            return None
        env = dict(os.environ)
        env["OTEL_SDK_DISABLED"] = "true"
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary, "-p", f"{system}\n\n{text}",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError:
            log.exception("claude binary could not be started")
            return None
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("claude completion timed out after %ss", self._timeout_s)
            return None
        if proc.returncode != 0:
            # A failing CLI prints its complaint on stdout (observed live:
            # usage-credit exhaustion exits 1). Only the exit code separates
            # an answer from an error — never surface stdout as a summary.
            log.warning(
                "claude completion failed rc=%s stderr=%r",
                proc.returncode,
                stderr.decode("utf-8", errors="replace")[:200],
            )
            return None
        result = stdout.decode("utf-8", errors="replace").strip()
        return result or None


__all__ = ["ClaudeCompleter"]
