from __future__ import annotations

from joat.agents.location_tools import wearer_location_tools
from joat.agents.tools import ToolBelt, ToolCall
from joat.core.navigation import NavigationCandidate, NavigationPoint


def _candidate() -> NavigationCandidate:
    return NavigationCandidate(
        choice_id="nearby-cafe",
        label="Lakeview Cafe",
        address="2400 N Clark St",
        point=NavigationPoint(41.92, -87.64),
    )


class TestWearerLocationTools:
    async def test_initial_toolbelt_exposes_nearby_and_refreshes_privately(self) -> None:
        refreshed = False
        searches: list[tuple[str, NavigationPoint]] = []

        async def refresh() -> tuple[float, float] | None:
            nonlocal refreshed
            refreshed = True
            return (41.9, -87.65)

        async def search(query: str, point: NavigationPoint) -> list[NavigationCandidate]:
            searches.append((query, point))
            return [_candidate()]

        belt = ToolBelt(wearer_location_tools(lambda: None, refresh, search))

        assert {schema["function"]["name"] for schema in belt.schemas()} == {
            "get_weather", "find_nearby_places",
        }
        result = await belt.execute(ToolCall("find_nearby_places", {"query": "coffee"}))
        assert refreshed is True
        assert searches == [("coffee", NavigationPoint(41.9, -87.65))]
        assert "Lakeview Cafe" in result
        assert "41.9" not in result
        assert "-87.65" not in result

    def test_no_provider_does_not_advertise_a_nearby_lookup(self) -> None:
        belt = ToolBelt(wearer_location_tools(lambda: None, _no_refresh, None))

        assert [schema["function"]["name"] for schema in belt.schemas()] == ["get_weather"]


async def _no_refresh() -> tuple[float, float] | None:
    raise AssertionError("weather was not invoked")
