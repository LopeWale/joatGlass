"""The first tools on the belt: weather and the wearer's messages.

Weather rides Open-Meteo — no API key, free for personal use, and the
engine already reaches the open web for the debate verifier, so this adds
no new network posture. Messages ride the chat.db reader that has been
parsing this data all along.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from ..channels.cards import CardLog
    from ..channels.imessage import IMessageStore

# WMO weather interpretation codes, the subset that matters when spoken.
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "icy fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def _fetch_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8.0) as resp:
        return json.loads(resp.read().decode())


def _forecast(latitude: float, longitude: float, name: str) -> str:
    fc = _fetch_json(
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode({
            "latitude": latitude, "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,weather_code,"
                       "wind_speed_10m,precipitation",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        })
    )
    cur = fc.get("current") or {}
    sky = _WMO.get(int(cur.get("weather_code", -1)), "unknown sky")
    return (
        f"Weather {name}: {sky}, {cur.get('temperature_2m')}F "
        f"(feels like {cur.get('apparent_temperature')}F), "
        f"wind {cur.get('wind_speed_10m')} mph"
    )


def _weather_sync(location: str) -> str:
    geo = _fetch_json(
        "https://geocoding-api.open-meteo.com/v1/search?"
        + urllib.parse.urlencode({"name": location, "count": 1})
    )
    hits = geo.get("results") or []
    if not hits:
        return f"error: no place called {location!r} found"
    place = hits[0]
    fc = _fetch_json(
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode({
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,"
                       "wind_speed_10m,precipitation",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        })
    )
    cur = fc.get("current") or {}
    sky = _WMO.get(int(cur.get("weather_code", -1)), "unknown sky")
    return (
        f"Weather in {place['name']}: {sky}, {cur.get('temperature_2m')}F "
        f"(feels like {cur.get('apparent_temperature')}F), "
        f"wind {cur.get('wind_speed_10m')} mph"
    )


CoordsProvider = Callable[[], "tuple[float, float] | None"]
RefreshCoordsProvider = Callable[[], Awaitable["tuple[float, float] | None"]]
_WEARER_LOCATION_SENTINELS = frozenset({
    "wearer",
    "wearer location",
    "my location",
    "current location",
    "here",
})
_WEARER_WEATHER_REQUESTS = frozenset({
    "weather",
    "weather outside",
    "weather here",
    "weather near me",
    "what's the weather",
    "what's the weather outside",
    "what's the weather here",
    "what is the weather",
    "what is the weather outside",
    "what is the weather here",
    "how's the weather",
    "how is the weather",
    "is it raining",
    "is it raining outside",
    "will it rain",
    "will it rain today",
    "temperature outside",
    "what's the temperature outside",
    "what is the temperature outside",
})


def weather_at_wearer_intent(utterance: str) -> dict[str, Any] | None:
    """Own only fixed, no-city current-weather requests.

    This avoids an otherwise mandatory local-model tool-decision pass for a
    request whose destination is already unambiguous: the existing weather
    tool obtains the phone location privately. Named places and every broader
    forecast question continue through model-directed tool selection.
    """
    request = " ".join(utterance.strip().casefold().rstrip("?!.,").split())
    return {} if request in _WEARER_WEATHER_REQUESTS else None


def weather_tool(
    coords: CoordsProvider | None = None,
    refresh_coords: RefreshCoordsProvider | None = None,
) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        location = str(args.get("location", "")).strip()
        if " ".join(location.casefold().split()) in _WEARER_LOCATION_SENTINELS:
            location = ""
        if location:
            return await asyncio.to_thread(_weather_sync, location)
        # No city named: use the wearer's reported position —
        # the forecast API takes lat/lon directly, no geocoding hop.
        here = coords() if coords is not None else None
        if here is None and refresh_coords is not None:
            here = await refresh_coords()
        if here is None:
            return "error: phone location is unavailable or stale"
        return await asyncio.to_thread(_forecast, here[0], here[1], "where you are")

    return Tool(
        name="get_weather",
        description=(
            "Current weather for a named place, or where the wearer is when "
            "location is omitted. Call this whenever the wearer asks about "
            "weather, rain, or temperature."
        ),
        parameters={
            "type": "object",
            "properties": {"location": {
                "type": "string",
                "description": "City or place name, e.g. 'Norman, Oklahoma'",
            }},
            "required": [],
        },
        run=run,
        explicit_intent=weather_at_wearer_intent,
        decision_hints=("weather", "rain", "raining", "temperature", "forecast", "wind"),
    )


def notifications_tool(log_: "CardLog") -> Tool:
    async def run(args: dict[str, Any]) -> str:
        entries = log_.recent(int(args.get("limit", 5) or 5))
        if not entries:
            return "no notifications shown recently"
        now = time.time()
        return "; ".join(
            f"[{max(0, int((now - c.at) / 60))}m ago] "
            f"{c.render().replace(chr(10), ' / ')}"
            for c in entries
        )

    return Tool(
        name="get_recent_notifications",
        description=(
            "Notifications recently shown on the wearer's glasses. Call "
            "this when they ask what they missed or what that alert was."
        ),
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": [],
        },
        run=run,
        decision_hints=("notification", "notifications", "alert", "alerts", "missed", "miss"),
    )


def messages_tool(store: "IMessageStore") -> Tool:
    def _age(unix: float) -> str:
        mins = max(0, int((time.time() - unix) / 60))
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        return f"{hours}h ago" if hours < 24 else f"{hours // 24}d ago"

    async def run(args: dict[str, Any]) -> str:
        try:
            limit = int(args.get("limit", 3))
        except (TypeError, ValueError):
            limit = 3
        try:
            rows = await asyncio.to_thread(store.recent, limit)
        except PermissionError:
            return (
                "Messages access unavailable. Check Mac Settings > Privacy & Security > "
                "Full Disk Access, then retry. No messages were read."
            )
        except (OSError, sqlite3.Error):
            return "Messages is temporarily unavailable. Retry shortly. No messages were read."
        if not rows:
            return "no recent messages"
        return "; ".join(
            f"{sender} ({_age(at)}): {text[:80]}" for sender, text, at in rows
        )

    return Tool(
        name="get_recent_messages",
        description=(
            "The wearer's most recent incoming text messages, newest per "
            "sender. Call this when they ask who messaged them or what "
            "someone said."
        ),
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "How many senders, max 10"}},
            "required": [],
        },
        run=run,
        decision_hints=("message", "messages", "text", "texts", "texted", "messaged", "said"),
    )
