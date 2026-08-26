"""Default fetchers for the Verifier Gate — stdlib only.

fetch_page_text: HTTP(S) GET with size cap and a real User-Agent, HTML
stripped to visible text. wayback_page_text: archive.org availability API →
fetch the newest snapshot. Both are FetchText-shaped (url → text, raise on
failure) so the gate never knows which one fed it.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (Macintosh) JOAT-VerifierGate/0.1"
MAX_BYTES = 2_000_000
TIMEOUT_S = 20.0

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks))


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read(MAX_BYTES)
        content_type = resp.headers.get("Content-Type", "")
    body = raw.decode("utf-8", errors="replace")
    return html_to_text(body) if "html" in content_type or body.lstrip()[:1] == "<" else body


async def fetch_page_text(url: str) -> str:
    return await asyncio.to_thread(_get, url)


async def wayback_page_text(url: str) -> str:
    """archive.org availability API → newest snapshot's text."""
    def lookup() -> str:
        req = urllib.request.Request(
            "https://archive.org/wayback/available?url=" + urllib.parse.quote(url, safe=""),
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
        snap = (data.get("archived_snapshots") or {}).get("closest") or {}
        snapshot_url = snap.get("url")
        if not snapshot_url or not snap.get("available"):
            raise LookupError("no wayback snapshot available")
        return str(snapshot_url)

    snapshot = await asyncio.to_thread(lookup)
    return await asyncio.to_thread(_get, snapshot)

