from __future__ import annotations

from joat.agents.nearby_places import nearby_place_intent, nearby_places_tool
from joat.core.navigation import NavigationCandidate, NavigationPoint


def _candidate(name: str, address: str) -> NavigationCandidate:
    return NavigationCandidate(
        choice_id=f"place-{name}",
        label=name,
        address=address,
        point=NavigationPoint(latitude=41.91, longitude=-87.64),
    )


class TestNearbyPlacesTool:
    def test_explicit_locality_intent_extracts_only_the_place_category(self) -> None:
        assert nearby_place_intent("coffee near me") == {"query": "coffee"}
        assert nearby_place_intent("Could you find pharmacies around here?") == {"query": "pharmacies"}
        assert nearby_place_intent("nearby restaurants") == {"query": "restaurants"}
        assert nearby_place_intent("could you find nearby restaurants?") == {"query": "restaurants"}
        assert nearby_place_intent("do you have nearby coffee shops?") == {"query": "coffee shops"}
        assert nearby_place_intent("where is the closest grocery store?") == {"query": "grocery store"}

    def test_bare_or_nonlocal_questions_do_not_become_implicit_searches(self) -> None:
        assert nearby_place_intent("what is nearby?") == {"query": ""}
        assert nearby_place_intent("tell me about coffee") is None

    async def test_refreshes_phone_location_then_searches_with_mac_only_coordinates(self) -> None:
        calls: list[tuple[str, NavigationPoint]] = []

        async def refresh() -> tuple[float, float] | None:
            return (41.9, -87.65)

        async def search(query: str, point: NavigationPoint) -> list[NavigationCandidate]:
            calls.append((query, point))
            return [_candidate("Cajun Corner", "2301 N Lincoln Ave")]

        tool = nearby_places_tool(lambda: None, refresh, search)

        result = await tool.run({"query": "restaurants"})

        assert calls == [("restaurants", NavigationPoint(41.9, -87.65))]
        assert "Cajun Corner" in result
        assert "2301 N Lincoln" in result
        assert "41.9" not in result
        assert "-87.65" not in result

    async def test_prefers_a_fresh_phone_fix_over_an_existing_cached_location(self) -> None:
        calls: list[tuple[str, NavigationPoint]] = []

        async def refresh() -> tuple[float, float] | None:
            return (41.91, -87.64)

        async def search(query: str, point: NavigationPoint) -> list[NavigationCandidate]:
            calls.append((query, point))
            return [_candidate("Fresh Cafe", "2400 N Clark St")]

        tool = nearby_places_tool(lambda: (41.9, -87.65), refresh, search)

        assert "Fresh Cafe" in await tool.run({"query": "coffee"})
        assert calls == [("coffee", NavigationPoint(41.91, -87.64))]

    async def test_uses_the_cached_fix_only_when_the_fresh_phone_request_fails(self) -> None:
        calls: list[NavigationPoint] = []

        async def refresh() -> tuple[float, float] | None:
            return None

        async def search(_query: str, point: NavigationPoint) -> list[NavigationCandidate]:
            calls.append(point)
            return [_candidate("Cached Cafe", "2400 N Clark St")]

        tool = nearby_places_tool(lambda: (41.9, -87.65), refresh, search)

        assert "Cached Cafe" in await tool.run({"query": "coffee"})
        assert calls == [NavigationPoint(41.9, -87.65)]

    async def test_never_searches_with_stale_or_missing_phone_location(self) -> None:
        searched = False

        async def refresh() -> tuple[float, float] | None:
            return None

        async def search(_query: str, _point: NavigationPoint) -> list[NavigationCandidate]:
            nonlocal searched
            searched = True
            return []

        result = await nearby_places_tool(lambda: None, refresh, search).run({"query": "coffee"})

        assert searched is False
        assert result == "error: phone location is unavailable or stale"

    async def test_rejects_an_empty_or_oversized_category_before_a_provider_call(self) -> None:
        async def refresh() -> tuple[float, float] | None:
            raise AssertionError("must not refresh for invalid input")

        async def search(_query: str, _point: NavigationPoint) -> list[NavigationCandidate]:
            raise AssertionError("must not search for invalid input")

        tool = nearby_places_tool(lambda: (41.9, -87.65), refresh, search)
        assert await tool.run({"query": ""}) == "error: say what kind of place to find"
        assert await tool.run({"query": "x" * 257}) == "error: nearby-place query is too long"
