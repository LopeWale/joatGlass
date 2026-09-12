"""Helpers for bounded, incremental Server-Sent Event parsing.

The parser intentionally accepts only protocol-shaped frames:
- UTF-8 text payloads
- `data:` lines coalesced into one event payload
- blank-line event boundaries

It is intentionally conservative: unknown lines are ignored and malformed
frames are rejected as protocol violations.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from ..core.a2a import MAX_BYTES
from ..core.a2a import A2AError, A2AErrorCode


MAX_EVENT_BYTES = MAX_BYTES


async def iter_sse_data(
    raw_events: AsyncIterator[bytes],
    *,
    max_bytes: int = MAX_EVENT_BYTES,
) -> AsyncIterator[str]:
    """Dispatch complete SSE events, bounded independently of TCP chunking.

    CR, LF, and split CRLF are line endings. A blank line is the sole event
    boundary; valid JSON on an unfinished data line is never publication.
    Hermes' keepalive and completion comments carry no application payload.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("SSE event bound must be positive")
    line = bytearray()
    data: list[str] = []
    data_bytes = 0
    skip_lf = False
    first_line = True
    async for chunk in raw_events:
        for byte in chunk:
            if skip_lf:
                skip_lf = False
                if byte == 10:
                    continue
            if byte not in (10, 13):
                line.append(byte)
                if len(line) > max_bytes:
                    raise A2AError(A2AErrorCode.PROTOCOL_ERROR)
                continue
            skip_lf = byte == 13
            try:
                value = line.decode("utf-8", "strict")
            except UnicodeError:
                raise A2AError(A2AErrorCode.PROTOCOL_ERROR) from None
            line.clear()
            if first_line:
                value = value.removeprefix("\ufeff")
                first_line = False
            if not value:
                if data:
                    yield "\n".join(data)
                data = []
                data_bytes = 0
                continue
            if value.startswith(":"):
                continue
            field, separator, payload = value.partition(":")
            if field != "data":
                continue
            if separator and payload.startswith(" "):
                payload = payload[1:]
            data_bytes += len(payload.encode("utf-8")) + (1 if data else 0)
            if data_bytes > max_bytes:
                raise A2AError(A2AErrorCode.PROTOCOL_ERROR)
            data.append(payload)
    if line or data:
        # A torn frame is not a completed answer, even if its prefix parses.
        raise A2AError(A2AErrorCode.PROTOCOL_ERROR)


def parse_sse_payload(payload: str) -> dict[str, object]:
    """Load one SSE payload JSON object.

    The helper stays in this module to keep transport parsing distinct from
    terminal logic, and to allow a unit boundary for malformed-frame failure.
    """
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise A2AError(A2AErrorCode.PROTOCOL_ERROR) from exc
    if not isinstance(value, dict):
        raise A2AError(A2AErrorCode.PROTOCOL_ERROR)
    return value
