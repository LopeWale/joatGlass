"""Verifier Gate v1 (ROADMAP Phase 2; ARCHITECTURE.md §5/§Verifier).

Metric it exists to enforce: **0 unverified citations ever pass the gate.**

Pipeline per citation: URL policy check → fetch the live page (Wayback
availability fallback) → normalized fuzzy quote-in-page match → verdict.
Any REQUIRED citation that is unverifiable ⇒ the whole submission is DENIED
with a structured repair request the agent can act on.

v1 scope, per roadmap: fetch + Wayback + quote match. The NLI claim-support
judge (MiniCheck) is the explicitly-second stage and slots in behind the
same Verdict shape.

Network access is injectable (`fetch_text`) so the gate's logic is fully
testable offline; the default fetcher lives in fetcher.py.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

log = logging.getLogger(__name__)

FetchText = Callable[[str], Awaitable[str]]
"""url → extracted page text. Raises on failure."""


@dataclass(frozen=True, slots=True)
class Citation:
    """One citation from an agent's output (ARCHITECTURE §4 schema)."""

    id: str
    url: str
    quote: str
    title: str = ""
    required: bool = True


class CitationStatus(Enum):
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class CitationVerdict:
    citation_id: str
    status: CitationStatus
    reason: str
    source: str = "live"  # live | wayback


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    citations: tuple[CitationVerdict, ...]
    repair_request: str = ""


def normalize(text: str) -> str:
    """Case, whitespace, and punctuation-insensitive form for matching.

    ALL punctuation collapses to spaces — including apostrophes: since the
    page and the quote go through the same normalizer, dropping information
    consistently beats preserving it inconsistently (smart quotes, hyphens
    and ellipses otherwise defeat verbatim matches)."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def quote_coverage(page: str, quote: str) -> float:
    """0..1 — how much of the quote appears contiguously in the page."""
    npage, nquote = normalize(page), normalize(quote)
    if not nquote:
        return 0.0
    if nquote in npage:
        return 1.0
    match = difflib.SequenceMatcher(None, npage, nquote, autojunk=False).find_longest_match(
        0, len(npage), 0, len(nquote)
    )
    return match.size / len(nquote)


class VerifierGate:
    """Composes fetch + fallback + matching into one verify() call."""

    def __init__(
        self,
        fetch_text: FetchText,
        wayback_text: FetchText | None = None,
        verified_threshold: float = 0.95,
        partial_threshold: float = 0.70,
        max_concurrent: int = 4,
    ) -> None:
        self._fetch = fetch_text
        self._wayback = wayback_text
        self._verified = verified_threshold
        self._partial = partial_threshold
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def verify(self, citations: list[Citation]) -> Verdict:
        verdicts = tuple(
            await asyncio.gather(*(self._verify_one(c) for c in citations))
        )
        failures = [
            v for v, c in zip(verdicts, citations, strict=True)
            if c.required and v.status is CitationStatus.UNVERIFIABLE
        ]
        if failures:
            lines = "\n".join(f"- {v.citation_id}: {v.reason}" for v in failures)
            return Verdict(
                passed=False,
                citations=verdicts,
                repair_request=(
                    "DENIED — the following required citations could not be "
                    f"verified. Replace them with citations whose quotes appear "
                    f"verbatim at the given URL, or drop the claims:\n{lines}"
                ),
            )
        return Verdict(passed=True, citations=verdicts)

    async def _verify_one(self, citation: Citation) -> CitationVerdict:
        policy = self._url_policy(citation.url)
        if policy is not None:
            return CitationVerdict(citation.id, CitationStatus.UNVERIFIABLE, policy)
        async with self._semaphore:
            page, source, fetch_error = await self._fetch_with_fallback(citation.url)
        if page is None:
            return CitationVerdict(
                citation.id, CitationStatus.UNVERIFIABLE,
                f"fetch failed (live{' and wayback' if self._wayback else ''}): {fetch_error}",
            )
        coverage = quote_coverage(page, citation.quote)
        if coverage >= self._verified:
            return CitationVerdict(
                citation.id, CitationStatus.VERIFIED,
                f"quote found ({coverage:.0%} contiguous match)", source,
            )
        if coverage >= self._partial:
            return CitationVerdict(
                citation.id, CitationStatus.PARTIALLY_VERIFIED,
                f"quote only partially present ({coverage:.0%})", source,
            )
        return CitationVerdict(
            citation.id, CitationStatus.UNVERIFIABLE,
            f"quote not found in page ({coverage:.0%} best match)", source,
        )

    async def _fetch_with_fallback(self, url: str) -> tuple[str | None, str, str]:
        try:
            return await self._fetch(url), "live", ""
        except Exception as live_exc:
            log.info("live fetch failed for %s: %s", url, live_exc)
            if self._wayback is None:
                return None, "live", str(live_exc)
            try:
                return await self._wayback(url), "wayback", ""
            except Exception as wb_exc:
                return None, "wayback", f"{live_exc}; wayback: {wb_exc}"

    @staticmethod
    def _url_policy(url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"disallowed scheme {parsed.scheme!r}"
        if not parsed.netloc or "." not in parsed.netloc:
            return "no resolvable host in URL"
        return None
