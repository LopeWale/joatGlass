"""The Search tab: "look up …", answered on the Mac, read on the glass."""

from __future__ import annotations

import pytest

from joat.services.search import SearchResult, SearchService, ddgs_search


def results() -> list[SearchResult]:
    return [
        SearchResult("Even Realities G2 review", "https://www.example.com/g2", "A long snippet " * 20),
        SearchResult("G2 on Wikipedia", "https://en.wikipedia.org/wiki/G2", ""),
    ]


def service(**kwargs) -> tuple[SearchService, list[str]]:
    queries: list[str] = []

    def search(query: str, max_results: int) -> list[SearchResult]:
        queries.append(query)
        if query == "nothing":
            return []
        if query == "boom":
            raise RuntimeError("engine down")
        return results()

    svc = SearchService(
        search=search,
        fetch_page=lambda url: ("Even Realities G2 review", "Body text. " * 100),
        **kwargs,
    )
    return svc, queries


class TestLookup:
    async def test_a_lookup_keeps_the_results_and_speaks_the_top_of_them(self) -> None:
        svc, queries = service()
        spoken = await svc.lookup("  Even Realities G2? ")
        assert queries == ["Even Realities G2"]
        assert spoken.startswith("For Even Realities G2, 1. Even Realities G2 review, example.com: A long snippet")
        assert "2. G2 on Wikipedia, en.wikipedia.org." in spoken
        snapshot = svc.snapshot()
        assert snapshot.headline == "Even Realities G2"
        assert snapshot.rows == (
            "Even Realities G2 review · example.com",
            "G2 on Wikipedia · en.wikipedia.org",
        )

    async def test_an_empty_query_asks_instead_of_searching(self) -> None:
        svc, queries = service()
        assert await svc.lookup(" ?") == "What should I look up?"
        assert queries == []
        assert svc.snapshot().empty == "Say “JOAT, look up …” to search."

    async def test_no_results_and_a_dead_engine_are_told_apart(self) -> None:
        svc, _ = service()
        assert await svc.lookup("nothing") == "I found nothing for nothing."
        assert svc.snapshot().empty == "No results — say a different query."
        assert await svc.lookup("boom") == "I couldn't reach a search engine for boom."
        assert svc.snapshot().empty == "Search unavailable — hold to retry."

    async def test_refresh_reruns_the_last_query(self) -> None:
        svc, queries = service()
        await svc.refresh()
        assert queries == []
        await svc.lookup("g2")
        await svc.refresh()
        assert queries == ["g2", "g2"]


class TestOpen:
    async def test_opening_a_result_reads_the_page_into_the_reader(self) -> None:
        svc, _ = service()
        await svc.lookup("g2")
        reader = await svc.open(0)
        assert reader is not None
        assert reader.title == "SEARCH · RESULT"
        assert reader.metadata == "example.com · Even Realities G2 review"
        assert reader.pages[0].startswith("Body text.")
        assert await svc.open(5) is None

    async def test_an_unreadable_page_falls_back_to_the_snippet(self) -> None:
        svc, _ = service()
        await svc.lookup("g2")
        svc._fetch_page = lambda url: (_ for _ in ()).throw(OSError())  # type: ignore[assignment]
        reader = await svc.open(0)
        assert reader is not None
        assert reader.pages[0].startswith("A long snippet")

    @pytest.mark.parametrize("body", [
        "  First  paragraph\r\n\r\n" + "界" * 440 + "\r\n  LATER MARKER  ",
        "🙂" * 320 + "  LATER MARKER\n",
    ])
    async def test_result_reader_retains_exact_fetched_text(self, body: str) -> None:
        svc, _ = service()
        svc._fetch_page = lambda url: ("Unicode result", body)  # type: ignore[assignment]
        await svc.lookup("g2")
        reader = await svc.open(0)
        assert reader is not None
        assert "".join(reader.pages) == body
        assert len(reader.pages) > 1


class TestRows:
    def test_a_row_fits_the_list_cap_and_names_the_domain(self) -> None:
        row = SearchResult("T" * 100, "https://www.example.com/x", "").row()
        assert len(row) <= 64
        assert row.endswith(" · example.com")


class TestDdgsAdapter:
    def test_maps_the_engine_shape_and_drops_junk(self, monkeypatch) -> None:
        class FakeDDGS:
            def __init__(self, timeout: int) -> None:
                assert timeout == 8

            def text(self, query: str, **kwargs: object) -> list[dict[str, object]]:
                return [
                    {"title": "  Good  result ", "href": "https://a/b", "body": "s"},
                    {"title": "", "href": "https://a/c", "body": ""},
                    {"title": "Bad link", "href": "javascript:void(0)"},
                ]

        import ddgs

        monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
        found = ddgs_search("q", 5)
        assert found == [SearchResult("Good result", "https://a/b", "s")]
