"""Verifier Gate v1 tests — offline, fetchers injected."""

from __future__ import annotations

import pytest

from joat.verifier.fetcher import html_to_text
from joat.verifier.gate import (
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


class TestCitationParser:
    def test_parses_sources_block_variants(self) -> None:
        from joat.verifier.citations import parse_citations
        prose = (
            "Some argument text.\n\nSOURCES:\n"
            '[1] https://a.example/x :: "quote one"\n'
            "[2] https://b.example/y — “curly quote two”\n"
            '[c3] https://c.example/z "no separator"\n'
            "not a citation line\n"
        )
        cites = parse_citations(prose)
        assert [(c.id, c.url, c.quote) for c in cites] == [
            ("1", "https://a.example/x", "quote one"),
            ("2", "https://b.example/y", "curly quote two"),
            ("c3", "https://c.example/z", "no separator"),
        ]

    def test_prose_without_sources_parses_empty(self) -> None:
        from joat.verifier.citations import parse_citations
        assert parse_citations("Just an opinion, no sources.") == []


class TestClaimExtraction:
    def test_finds_sentence_with_marker_excluding_sources(self) -> None:
        from joat.verifier.claims import claim_for_citation
        prose = (
            "The G2 display is monochrome [1]. It also has a mic array [2].\n\n"
            "SOURCES:\n[1] https://a.example/x :: \"monochrome green\"\n"
        )
        assert claim_for_citation(prose, "1") == "The G2 display is monochrome."
        assert claim_for_citation(prose, "2") == "It also has a mic array."
        assert claim_for_citation(prose, "9") is None


class FakeClaimJudge:
    def __init__(self, score: float | None) -> None:
        self.score = score
        self.asked: list[tuple[str, str]] = []

    async def support(self, doc: str, claim: str) -> float | None:
        self.asked.append((doc, claim))
        return self.score


CITED_PROSE = (
    "The firmware is signed with ECDSA-P256 [1].\n\nSOURCES:\n"
    '[1] https://a.example/spec :: "signed with ECDSA-P256"\n'
)


class TestNliStage:
    async def test_supported_claim_stays_verified_with_score_shown(self) -> None:
        judge = FakeClaimJudge(0.92)
        gate = VerifierGate(fake_fetcher({"https://a.example/spec": PAGE}), claim_judge=judge)
        verdict = await gate.verify(
            [Citation("1", "https://a.example/spec", "signed with ECDSA-P256")],
            prose=CITED_PROSE,
        )
        v = verdict.citations[0]
        assert v.status is CitationStatus.VERIFIED
        assert "claim support 0.92" in v.reason
        assert judge.asked and "firmware is signed" in judge.asked[0][1]

    async def test_real_quote_weak_claim_downgrades_visibly(self) -> None:
        gate = VerifierGate(
            fake_fetcher({"https://a.example/spec": PAGE}), claim_judge=FakeClaimJudge(0.12)
        )
        verdict = await gate.verify(
            [Citation("1", "https://a.example/spec", "signed with ECDSA-P256")],
            prose=CITED_PROSE,
        )
        v = verdict.citations[0]
        assert v.status is CitationStatus.PARTIALLY_VERIFIED
        assert "weakly supported" in v.reason
        assert verdict.passed  # downgraded and flagged, not denied (v1 policy)

    async def test_judge_failure_never_counts_as_support(self) -> None:
        gate = VerifierGate(
            fake_fetcher({"https://a.example/spec": PAGE}), claim_judge=FakeClaimJudge(None)
        )
        verdict = await gate.verify(
            [Citation("1", "https://a.example/spec", "signed with ECDSA-P256")],
            prose=CITED_PROSE,
        )
        v = verdict.citations[0]
        assert v.status is CitationStatus.VERIFIED  # quote stands on its own
        assert "claim support" not in v.reason      # no fabricated score

    async def test_no_prose_skips_nli_silently(self) -> None:
        judge = FakeClaimJudge(0.9)
        gate = VerifierGate(fake_fetcher({"https://a.example/spec": PAGE}), claim_judge=judge)
        verdict = await gate.verify([Citation("1", "https://a.example/spec", "signed with ECDSA-P256")])
        assert judge.asked == []
        assert verdict.passed
