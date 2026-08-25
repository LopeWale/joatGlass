"""Parse citations out of streamed debate prose.

The debate stays PROSE so it streams to the HUD (the wearer's visibility
requirement) — structure rides in a SOURCES block the agents are prompted
to append:

    SOURCES:
    [1] https://example.com/page :: "the exact verbatim quote"
    [2] https://other.example/x "another quote"

Lines tolerate '::', '-', or nothing between URL and quote, and straight or
curly quotes. Anything unparseable is simply not a citation — the gate then
has nothing to check for it, and the NLI stage (roadmap: second) is where
uncited claims get caught.
"""

from __future__ import annotations

import re

from .gate import Citation

_LINE = re.compile(
    r"^\s*\[(?P<id>[^\]]{1,16})\]\s+(?P<url>https?://\S+?)\s*(?:::|[-–—])?\s*"
    r"[\"“](?P<quote>.+?)[\"”]\s*$",
    re.MULTILINE,
)


def parse_citations(prose: str) -> list[Citation]:
    return [
        Citation(id=m.group("id"), url=m.group("url"), quote=m.group("quote"))
        for m in _LINE.finditer(prose)
    ]
