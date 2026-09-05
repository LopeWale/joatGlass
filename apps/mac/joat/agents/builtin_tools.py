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


def _message_age(unix: float) -> str:
    mins = max(0, int((time.time() - unix) / 60))
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    return f"{hours}h ago" if hours < 24 else f"{hours // 24}d ago"


MESSAGES_PERMISSION_NOTICE = (
    "Messages access unavailable. Check Mac Settings > Privacy & Security > "
    "Full Disk Access, then retry. No messages were read."
)
MESSAGES_UNAVAILABLE_NOTICE = (
    "Messages is temporarily unavailable. Retry shortly. No messages were read."
)
# A conversation read is the wearer's own history; the reader on the lens
# already shows whole messages, so the mind gets the same, bounded so one
# chatty thread cannot swallow the reply loop. Newest messages win the cut.
CONVERSATION_SNIPPET_CHARS = 160
CONVERSATION_READ_CHARS = 2_400


def messages_tool(store: "IMessageStore") -> Tool:
    async def run(args: dict[str, Any]) -> str:
        try:
            limit = int(args.get("limit", 3))
        except (TypeError, ValueError):
            limit = 3
        try:
            rows = await asyncio.to_thread(store.recent, limit)
        except PermissionError:
            return MESSAGES_PERMISSION_NOTICE
        except (OSError, sqlite3.Error):
            return MESSAGES_UNAVAILABLE_NOTICE
        if not rows:
            return "no recent messages"
        return "; ".join(
            f"{sender} ({_message_age(at)}): "
            f"{text[:CONVERSATION_SNIPPET_CHARS]}"
            f"{'…' if len(text) > CONVERSATION_SNIPPET_CHARS else ''}"
            for sender, text, at in rows
        )

    return Tool(
        name="get_recent_messages",
        description=(
            "The wearer's most recent incoming text messages, newest per "
            "sender, as short snippets. Call this when they ask who messaged "
            "them. To read what someone actually said, in full, call "
            "read_conversation with that sender's name."
        ),
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "How many senders, max 10"}},
            "required": [],
        },
        run=run,
        decision_hints=("message", "messages", "text", "texts", "texted", "messaged", "said"),
    )


def conversation_tool(store: "IMessageStore") -> Tool:
    """Whole messages from one conversation, the way the lens reader shows them.

    The audit's M8: the mind saw eighty characters per sender and nothing
    else, so "read me Dele's message" answered with a snippet and an
    apology. This reads the same Mac-owned thread the Messages feature
    opens, oldest to newest, complete text, bounded by characters rather
    than by cutting a sentence in half.
    """

    async def run(args: dict[str, Any]) -> str:
        name = str(args.get("name", "") or "").strip()
        if not name:
            return "error: name is required — whose conversation to read"
        try:
            count = int(args.get("messages", 8))
        except (TypeError, ValueError):
            count = 8
        count = max(1, min(count, 16))
        try:
            threads = await asyncio.to_thread(store.recent_threads, 10)
        except PermissionError:
            return MESSAGES_PERMISSION_NOTICE
        except (OSError, sqlite3.Error):
            return MESSAGES_UNAVAILABLE_NOTICE
        needle = name.casefold()
        matches = [t for t in threads if needle in t.sender.casefold()]
        exact = [t for t in matches if t.sender.casefold() == needle]
        if exact:
            matches = exact
        if not matches:
            senders = ", ".join(t.sender for t in threads) or "none"
            return f"no recent conversation with {name!r}; recent conversations: {senders}"
        if len(matches) > 1:
            return "several conversations match — say which: " + ", ".join(t.sender for t in matches)
        thread = matches[0]
        try:
            page = await asyncio.to_thread(store.thread_message_page, thread, limit=count)
        except PermissionError:
            return MESSAGES_PERMISSION_NOTICE
        except (OSError, sqlite3.Error):
            return MESSAGES_UNAVAILABLE_NOTICE
        # The page arrives newest first; the mind reads it like a transcript.
        entries = list(reversed(page.entries))
        if not entries:
            return f"the conversation with {thread.sender} has no readable messages"
        lines: list[str] = []
        for entry in entries:
            who = "You" if entry.from_me else (entry.sender or thread.sender)
            text = entry.text or ("[attachment]" if entry.attachment_count else "")
            lines.append(f"{who} ({_message_age(entry.at)}): {text}")
        body = "\n".join(lines)
        truncated = False
        while len(body) > CONVERSATION_READ_CHARS and len(lines) > 1:
            lines.pop(0)
            body = "\n".join(lines)
            truncated = True
        if len(body) > CONVERSATION_READ_CHARS:
            body = "…" + body[-CONVERSATION_READ_CHARS:]
            truncated = True
        kind = "group conversation" if thread.is_group else "conversation"
        notes = []
        if page.has_older or truncated:
            notes.append("older messages exist")
        notes.append(
            "a reply can be prepared from the glasses" if thread.reply_target is not None
            else "replying from the glasses is not available for this conversation"
        )
        return f"{kind} with {thread.sender} ({'; '.join(notes)}):\n{body}"

    return Tool(
        name="read_conversation",
        description=(
            "Read the wearer's recent messages with one person or group, in "
            "full and in order. Call this when they ask what someone said, "
            "to read a message, or for the whole thread. Takes the sender's "
            "name as it appears in Messages."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The sender or group name, as spoken."},
                "messages": {
                    "type": "integer",
                    "description": "How many of the newest messages to read, 1 to 16 (default 8)",
                },
            },
            "required": ["name"],
        },
        run=run,
        decision_hints=(
            "message", "messages", "text", "texts", "said", "read", "conversation",
            "thread", "in full", "whole",
        ),
    )
