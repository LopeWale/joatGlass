"""Street surroundings for the guidance HUD, rendered on this Mac.

A route drawn as a bare polyline on black tells the wearer the SHAPE of
their path and nothing about where they are: no cross-streets, no blocks,
no park. This module fetches the map around the wearer from Mapbox Static
Images, converts it for a monochrome-green optical HUD, and hands back the
bytes plus the exact projection parameters the plugin needs to draw JOAT's
own route on top, aligned to the pixel.

Two properties matter more than fidelity:

* **Black must stay black.** On the G2 a black pixel is transparent glass.
  The tone curve maps the map's land tone to zero and stretches only the
  road tones, so roughly a seventh of the panel lights up instead of the
  whole rectangle glowing.
* **The token never leaves this Mac.** The plugin receives pixels, not a
  URL, so the access token is never on the wire, in the HUD, or in a log.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import io
import logging
import math
import urllib.error
import urllib.parse
import urllib.request

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image as ImageType

log = logging.getLogger(__name__)

STATIC_URL = "https://api.mapbox.com/styles/v1/mapbox"
# Roads read light against dark land in this style, which is exactly the
# polarity a transparent HUD needs before any tone curve is applied.
MAP_STYLE = "navigation-night-v1"
# ~230 m across the panel: close enough to name the cross-street you are
# standing at, wide enough to see the next two blocks.
MAP_ZOOM = 16
# The map owns the right half down to the progress bar: the biggest window
# the frame can give it, because street names are the point.
MAP_WIDTH = 264
MAP_HEIGHT = 194
# Mapbox Static Images works in 512-pixel tiles; the plugin must use the
# same constant or the route it draws will not sit on the drawn roads.
MAP_TILE_PIXELS = 512

HttpGet = Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class NavigationMapTile:
    """One rendered map panel and the projection that produced it."""

    latitude: float
    longitude: float
    zoom: int
    width: int
    height: int
    png: bytes


class MapImageError(RuntimeError):
    """A bounded, token-free map failure. Guidance continues without a map."""


def _tone_curve(image: "ImageType") -> "ImageType":
    """Map land to black and stretch the road tones across the full range.

    The thresholds are measured from THIS tile rather than fixed, because a
    dense downtown and a park have very different tone distributions and a
    constant cutoff would black out one or flood the other.
    """
    histogram = image.histogram()
    total = sum(histogram) or 1
    accumulated = 0
    low = 0
    high = 255
    for value, count in enumerate(histogram):
        accumulated += count
        fraction = accumulated / total
        if low == 0 and fraction >= 0.80:
            low = value
        if fraction >= 0.99:
            high = value
            break
    if high <= low:
        high = min(255, low + 1)
    span = high - low

    def curve(value: int) -> int:
        if value <= low:
            return 0
        if value >= high:
            return 255
        return int(255 * (value - low) / span)

    stretched = image.point(curve)
    # Quantize to the sixteen levels the G2 protocol can actually carry, so
    # what the Mac previews is what the lens shows.
    def quantize(value: int) -> int:
        return int(round(int(value) / 255 * 15)) * 17

    return stretched.point(quantize)


async def render_map_tile(
    access_token: str,
    latitude: float,
    longitude: float,
    *,
    zoom: int = MAP_ZOOM,
    width: int = MAP_WIDTH,
    height: int = MAP_HEIGHT,
    http_get: HttpGet | None = None,
) -> NavigationMapTile:
    """Fetch and prepare the surroundings around one point."""
    if not access_token.strip():
        raise MapImageError("Mapbox access token is required")
    fetch = http_get or _stdlib_get
    url = (
        f"{STATIC_URL}/{MAP_STYLE}/static/"
        # 1x, NOT @2x. Mapbox sizes street labels for the requested pixel
        # density; asking for @2x and shrinking it back rendered every name
        # at double size and then destroyed it in the downsample, which is
        # why the panel had roads but no street names on it.
        f"{longitude:.6f},{latitude:.6f},{zoom},0/{width}x{height}"
        f"?access_token={urllib.parse.quote(access_token)}"
        "&attribution=false&logo=false"
    )
    raw = await fetch(url)
    return await asyncio.to_thread(
        _prepare, raw, latitude, longitude, zoom, width, height,
    )


def _prepare(
    raw: bytes,
    latitude: float,
    longitude: float,
    zoom: int,
    width: int,
    height: int,
) -> NavigationMapTile:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise MapImageError("Pillow is required to render map tiles") from exc
    try:
        source = Image.open(io.BytesIO(raw)).convert("L")
    except Exception as exc:
        raise MapImageError("Mapbox returned an unreadable image") from None
    # The tile already arrives at panel size; resampling here would blur the
    # very glyphs the wearer needs. Only correct a size mismatch.
    resized = (
        source if source.size == (width, height)
        else source.resize((width, height), Image.Resampling.LANCZOS)
    )
    prepared = _tone_curve(resized)
    buffer = io.BytesIO()
    prepared.save(buffer, format="PNG", optimize=True)
    return NavigationMapTile(
        latitude=latitude,
        longitude=longitude,
        zoom=zoom,
        width=width,
        height=height,
        png=buffer.getvalue(),
    )


async def _stdlib_get(url: str) -> bytes:
    return await asyncio.to_thread(_blocking_get, url)


def _blocking_get(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"Accept": "image/png", "User-Agent": "JOAT-G2/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        # The URL carries the access token; only the status code is safe.
        raise MapImageError(f"Mapbox map HTTP failure ({exc.code})") from None
    except (urllib.error.URLError, TimeoutError):
        raise MapImageError("Mapbox map request failed") from None


def map_move_meters(
    from_latitude: float,
    from_longitude: float,
    to_latitude: float,
    to_longitude: float,
) -> float:
    """Great-circle-ish distance, good to well under a metre at HUD scale."""
    mean_latitude = math.radians((from_latitude + to_latitude) / 2)
    dx = math.radians(to_longitude - from_longitude) * math.cos(mean_latitude)
    dy = math.radians(to_latitude - from_latitude)
    return math.hypot(dx, dy) * 6_371_000
