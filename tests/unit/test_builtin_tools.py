from __future__ import annotations

import pytest

from joat.agents.builtin_tools import weather_tool
from joat.agents.tools import ToolBelt


@pytest.mark.parametrize(
    "utterance",
    [
        "weather outside",
        "What's the weather here?",
        "is it raining?",
        "Will it rain today?",
        "temperature outside",
    ],
)
def test_explicit_wearer_weather_bypasses_the_model_decision(utterance: str) -> None:
    belt = ToolBelt([weather_tool()])

    call = belt.explicit_call(utterance)

    assert call is not None
    assert call.name == "get_weather"
    assert call.arguments == {}


@pytest.mark.parametrize(
    "utterance",
    [
        "weather in Paris",
        "will it rain in Chicago tomorrow",
        "tell me about weather models",
        "is rain good for tomatoes",
    ],
)
def test_named_or_ambiguous_weather_stays_with_the_model_decision(utterance: str) -> None:
    assert ToolBelt([weather_tool()]).explicit_call(utterance) is None


class TestWeatherToolLocationContract:
    async def test_schema_allows_omitted_location_and_refreshes_phone_coordinates(self, monkeypatch) -> None:
        requested = False

        async def refresh() -> tuple[float, float] | None:
            nonlocal requested
            requested = True
            return (41.9, -87.65)

        def forecast(latitude: float, longitude: float, name: str) -> str:
            assert (latitude, longitude, name) == (41.9, -87.65, "where you are")
            return "Weather where you are: clear"

        monkeypatch.setattr("joat.agents.builtin_tools._forecast", forecast)
        tool = weather_tool(lambda: None, refresh)

        assert tool.parameters["required"] == []
        assert await tool.run({}) == "Weather where you are: clear"
        assert requested is True

    async def test_wearer_sentinel_uses_fresh_phone_fix_not_named_place_geocoding(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "joat.agents.builtin_tools._weather_sync",
            lambda _value: (_ for _ in ()).throw(AssertionError("must not geocode sentinel")),
        )
        monkeypatch.setattr(
            "joat.agents.builtin_tools._forecast",
            lambda _lat, _lon, _name: "Weather where you are: clear",
        )
        tool = weather_tool(lambda: (41.9, -87.65))

        assert await tool.run({"location": "wearer location"}) == "Weather where you are: clear"
