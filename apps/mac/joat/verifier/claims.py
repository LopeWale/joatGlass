"""Extract the claim sentence behind each citation marker.

The quote check proves the cited words exist; the NLI check needs the
CLAIM — the sentence in the agent's own prose that carries the [n] marker.
That is what a misleading citation misrepresents: real quote, wrong claim.
"""

from __future__ import annotations

import re

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def claim_for_citation(prose: str, citation_id: str) -> str | None:
    """The sentence(s) containing [citation_id], SOURCES block excluded."""
    body = re.split(r"^\s*SOURCES\s*:", prose, maxsplit=1, flags=re.MULTILINE | re.IGNORECASE)[0]
    marker = f"[{citation_id}]"
    sentences = [s for s in _SENTENCE_SPLIT.split(body) if marker in s]
    if not sentences:
        return None
    cleaned = " ".join(s.replace(marker, "").strip() for s in sentences)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)  # "monochrome ." → "monochrome."
    return cleaned.strip() or None
