"""The margin facts on JOAT's resting dashboard: weather and unread count.

The Even Hub SDK gives a plugin location, device, glasses, user, and local
storage — and no weather (verified against the installed 0.0.14 typings).
Even's own dashboard can show a temperature because it is first-party. JOAT
gets there a different way: the phone already sends its position for
navigation and grounding, so the MAC resolves current conditions for that
position and sends a finished line to the lenses.

Kept deliberately small: one number, one short sky word, one count. A
resting HUD is a glance, not a weather app, and every lit pixel is power.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from ..core.interfaces import GlassesTransport
from ..core.protocol import HudAmbient

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
#: Re-ask no more often than this. Weather does not change in a minute, and
#: the wearer's battery pays for every request the Mac makes on their behalf.
REFRESH_SECONDS = 900.0
#: A fix this old stops being "here"; better to show nothing than a lie.
POSITION_MAX_AGE_SECONDS = 3_600.0

# WMO codes, reduced to words that fit a HUD margin. The spoken weather tool
# keeps the long forms; a dashboard has room for one.
_SKY: dict[int, str] = {
    0: "clear", 1: "clear", 2: "cloudy", 3: "overcast",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    56: "freezing", 57: "freezing",
    61: "rain", 63: "rain", 65: "heavy rain",
    66: "freezing", 67: "freezing",
    71: "snow", 73: "snow", 75: "heavy snow", 77: "snow",
    80: "showers", 81: "showers", 82: "storm",
    85: "snow", 86: "snow",
    95: "storm", 96: "storm", 99: "storm",
}


class AmbientPublisher:
    """Owns the dashboard's weather line and unread count."""

    def __init__(
        self,
        transport: GlassesTransport,
        *,
        unread_count: Callable[[], int] = lambda: 0,
        fahrenheit: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._unread_count = unread_count
        self._fahrenheit = fahrenheit
        self._monotonic = monotonic
        self._position: tuple[float, float] | None = None
        self._position_at: float | None = None
        self._temperature = ""
        self._condition = ""
        self._fetched_at: float | None = None
        self._task: asyncio.Task[None] | None = None

    def accept_position(self, latitude: float, longitude: float) -> None:
        """Take the phone's position; refresh the sky if it is time."""
        self._position = (latitude, longitude)
        self._position_at = self._monotonic()
        if self._should_refresh():
            self._start_refresh()

    async def publish(self) -> None:
        """Send the current margin facts, weather included when known."""
        if self._position_at is not None and (
            self._monotonic() - self._position_at > POSITION_MAX_AGE_SECONDS
        ):
            # The wearer has moved on from wherever this was measured.
            self._temperature = ""
            self._condition = ""
        await self._transport.send(HudAmbient(
            temperature=self._temperature,
            condition=self._condition,
            unread=max(0, min(999, self._unread_count())),
        ))

    def _should_refresh(self) -> bool:
        if self._task is not None and not self._task.done():
            return False
        if self._fetched_at is None:
            return True
        return self._monotonic() - self._fetched_at >= REFRESH_SECONDS

    def _start_refresh(self) -> None:
        position = self._position
        if position is None:
            return

        async def refresh() -> None:
            try:
                temperature, condition = await asyncio.to_thread(
                    _current_conditions, position[0], position[1], self._fahrenheit,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                log.info("ambient weather unavailable: %s", type(exc).__name__)
                return
            self._temperature = temperature
            self._condition = condition
            self._fetched_at = self._monotonic()
            await self.publish()

        self._task = asyncio.get_running_loop().create_task(refresh())


def _current_conditions(
    latitude: float, longitude: float, fahrenheit: bool,
) -> tuple[str, str]:
    query = urllib.parse.urlencode({
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "current": "temperature_2m,weather_code",
        "temperature_unit": "fahrenheit" if fahrenheit else "celsius",
    })
    request = urllib.request.Request(
        f"{FORECAST_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "JOAT-G2/0.1"},
    )
    with urllib.request.urlopen(request, timeout=8.0) as response:
        payload = json.load(response)
    current = payload.get("current") or {}
    raw = current.get("temperature_2m")
    if raw is None:
        raise ValueError("forecast carried no temperature")
    unit = "f" if fahrenheit else "c"
    temperature = f"{round(float(raw))}{unit}"
    condition = _SKY.get(int(current.get("weather_code", -1)), "")
    return temperature, condition
