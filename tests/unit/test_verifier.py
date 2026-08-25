"""Verifier Gate v1 tests — offline, fetchers injected."""

from __future__ import annotations

import pytest

from jarvis.verifier.fetcher import html_to_text
from jarvis.verifier.gate import (
    Citation,
    CitationStatus,
    VerifierGate,
    normalize,
    quote_coverage,
)

PAGE = (
    "The Even Realities G2 uses a 576 by 288 monochrome green display. "
    "Its firmware images are signed with ECDSA-P256, which blocks custom "
    "firmware entirely. The glasses pair over Bluetooth Low Energy."
)


def fake_fetcher(pages: dict[str, str]):
    async def fetch(url: str) -> str:
        if url in pages:
            return pages[url]
        raise ConnectionError(f"no route to {url}")
    return fetch


class TestMatching:
    def test_normalize_survives_copyediting(self) -> None:
        assert normalize("“ECDSA-P256,”  blocks…") == normalize("ecdsa p256 blocks")

    def test_exact_quote_scores_full(self) -> None:
        assert quote_coverage(PAGE, "signed with ECDSA-P256") == 1.0

    def test_fabricated_quote_scores_low(self) -> None:
        assert quote_coverage(PAGE, "the display is full color RGB") < 0.7


class TestGate:
    async def test_verbatim_citation_passes(self) -> None:
        gate = VerifierGate(fake_fetcher({"https://a.example/spec": PAGE}))
        verdict = await gate.verify(
            [Citation("c1", "https://a.example/spec", "blocks custom firmware entirely")]
        )
        assert verdict.passed
        assert verdict.citations[0].status is CitationStatus.VERIFIED

    async def test_fabricated_quote_denies_with_repair(self) -> None:
        gate = VerifierGate(fake_fetcher({"https://a.example/spec": PAGE}))
        verdict = await gate.verify(
            [Citation("c1", "https://a.example/spec", "the G2 supports full color video")]
        )
        assert not verdict.passed
        assert "DENIED" in verdict.repair_request and "c1" in verdict.repair_request

    async def test_dead_url_falls_back_to_wayback(self) -> None:
        gate = VerifierGate(
            fake_fetcher({}),  # live: everything fails
            wayback_text=fake_fetcher({"https://gone.example/doc": PAGE}),
        )
        verdict = await gate.verify(
            [Citation("c1", "https://gone.example/doc", "pair over Bluetooth Low Energy")]
        )
        assert verdict.passed
        assert verdict.citations[0].source == "wayback"

    async def test_dead_everywhere_is_unverifiable(self) -> None:
        gate = VerifierGate(fake_fetcher({}), wayback_text=fake_fetcher({}))
        verdict = await gate.verify([Citation("c1", "https://gone.example/x", "anything")])
        assert not verdict.passed
        assert verdict.citations[0].status is CitationStatus.UNVERIFIABLE

    async def test_optional_citation_failure_does_not_deny(self) -> None:
        gate = VerifierGate(fake_fetcher({"https://a.example/spec": PAGE}))
        verdict = await gate.verify([
            Citation("good", "https://a.example/spec", "monochrome green display"),
            Citation("weak", "https://a.example/spec", "totally absent claim", required=False),
        ])
        assert verdict.passed  # the optional failure is recorded but not fatal
        statuses = {v.citation_id: v.status for v in verdict.citations}
        assert statuses["weak"] is CitationStatus.UNVERIFIABLE

    @pytest.mark.parametrize("url", ["ftp://x.example/a", "javascript:alert(1)", "https://nohost"])
    async def test_url_policy_rejects_bad_schemes_and_hosts(self, url: str) -> None:
        gate = VerifierGate(fake_fetcher({}))
        verdict = await gate.verify([Citation("c1", url, "whatever")])
        assert verdict.citations[0].status is CitationStatus.UNVERIFIABLE
        assert not verdict.passed

    async def test_partial_match_is_partially_verified_and_passes(self) -> None:
        gate = VerifierGate(
            fake_fetcher({"https://a.example/spec": PAGE}),
            verified_threshold=0.95, partial_threshold=0.5,
        )
        verdict = await gate.verify(
            [Citation("c1", "https://a.example/spec",
                      "firmware images are signed with ECDSA-P256 and reviewed daily")]
        )
        assert verdict.citations[0].status is CitationStatus.PARTIALLY_VERIFIED
        assert verdict.passed


class TestHtmlToText:
    def test_strips_scripts_and_collapses(self) -> None:
        html = "<html><head><script>evil()</script></head><body><p>Real  text</p><style>x{}</style></body></html>"
        assert html_to_text(html) == "Real text"
