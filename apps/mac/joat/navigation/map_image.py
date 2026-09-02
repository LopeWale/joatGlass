"""Street surroundings for the guidance HUD, rendered on this Mac.

A route drawn as a bare polyline on black tells the wearer the SHAPE of
their path and nothing about where they are: no cross-streets, no blocks,
no park. This module fetches the map around the wearer from Mapbox Static
Images, converts it for a monochrome-green optical HUD, and hands back the
bytes plus the exact projection parameters the plugin needs to draw JOAT's
own route on top, aligned to the pixel.

Two properties matter more than fidelity:

* **Tone is the information.** A black pixel is transparent glass, and an
  early version of this used that as a reason to minimise lit pixels — it
  thresholded land to black and shipped a wireframe. On a map, tone IS the
  content: blocks, parks and water only exist as areas. The curve below
  spreads the tile across the map's level budget instead, which the measured
  LZ4 sizes show costs about a kilobyte.
* **The map does not get all sixteen levels.** It gets 3–11; JOAT keeps the
  top for the route, the position marker, and anything else it draws over
  the streets. A route that cannot out-shine a road is not a route.
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
# The map is FULL-BLEED in its pane, like the reference HUD: it runs to the
# right edge of the canvas and down to the fact line, with no border and no
# inner padding. A map with a frame around it is a picture of a map.
MAP_WIDTH = 284
MAP_HEIGHT = 208
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


#: The map's three bands, and the headroom above them.
#:
#: Read off the reference HUD rather than reasoned from first principles,
#: because reasoning got this wrong twice in opposite directions. On that
#: display the ground is a MID tone — roads sit brighter than it, buildings
#: and label glyphs sit darker than it. Three bands around a mid anchor.
#:
#: Both failures were a collapse of that structure into two bands:
#: thresholding land to black left a wireframe with no blocks, and later
#: flattening the ground to 0 lost the middle band the same way, while an
#: earlier floor-of-3-on-everything pushed all three bands up into a slab.
#: Ground tone is what makes blocks, parks and water read as AREAS.
#:
#: The ceiling is a budget, not a range: the map stops at 11 so JOAT keeps
#: 12–15 for the route and the position marker. A route that cannot
#: out-shine a road is not a route.
#:
#: The ground is UNLIT, and everything the map draws sits above it. This is
#: the polarity of the reference map, and the reason is legibility of the
#: one thing the wearer reads most: a street NAME. Mapbox draws labels as
#: the brightest ink in the tile, so with an unlit ground they land at the
#: top of the band and read instantly. Lighting the ground inverts that —
#: the label becomes a dark glyph on a lit field, which is the lowest
#: contrast pairing the panel can produce, and the names disappeared.
#:
#: A mid-tone ground was tried to give blocks "area", and it cost exactly
#: that: the names went unreadable. Buildings still read here, because they
#: are brighter than bare land in the source tile and get their own step in
#: the band below the roads.
MAP_GROUND_LEVEL = 0
MAP_TONE_FLOOR = 1
MAP_TONE_CEILING = 11

#: Expands the dim end of the ink band. Below 1 lifts the tones just above
#: bare land — which is where the road network lives — without disturbing
#: the order the source style already puts buildings, roads and labels in.
#: A straight linear stretch measured 18.3% of a tile one step above the
#: ground and 1.6% across the remaining six, so the streets were the
#: flattest thing on the map.
_TONE_GAMMA = 0.5


def _tone_curve(image: "ImageType") -> "ImageType":
    """Drop bare land to unlit; rank everything drawn on it across the band.

        bare land (the mode) -> MAP_GROUND_LEVEL, i.e. clear glass
        buildings/roads/labels -> MAP_TONE_FLOOR .. MAP_TONE_CEILING, by RANK

    Anchoring on the MODE rather than the minimum is what makes this hold
    across tiles: a dense downtown tile and an open suburban one have very
    different means, but in both, bare land is the tone covering the most
    pixels, so the ink lands in the same band on both.

    Ranking rather than stretching linearly is what separates the ink. A
    linear stretch measured 68.8% of a real tile at the ground level, 18.3%
    one step above it, and 1.6% across the remaining six: a handful of
    near-white label pixels set the top of the range and squashed every
    road into the bottom of the band, so the streets — the thing being read
    — were the flattest part of the map. Rank spends the band on the tones
    that actually cover the panel, which is what pulls buildings, roads and
    labels onto their own steps.

    Cost is measured, not assumed: `updateImageRawData` is LZ4-compressed
    by the SDK (0.0.12+) and flat quantized regions compress well — a real
    tile measured 5,850 B full-tone against 4,784 B thresholded, while
    Floyd-Steinberg dithering cost 22,345 B and buys nothing at four bits.
    """
    histogram = image.histogram()
    ground = max(range(len(histogram)), key=lambda value: histogram[value])
    occupied = [value for value, count in enumerate(histogram) if count]
    # Read the extremes off the histogram already in hand: getextrema() is
    # typed for multi-band images and needs unwrapping for no benefit here.
    low = min(occupied, default=ground)
    high = max(occupied, default=ground)
    if low == high:
        # A single-tone tile carries no map at all — a blank or failed
        # fetch. Anchoring it to the ground level would hang a solid lit
        # rectangle on the lens to say nothing; render nothing instead.
        return image.point(lambda _: 0)

    span = max(1, high - ground)

    def curve(value: int) -> int:
        if value <= ground:
            # Bare land: the wearer is already looking at this through the
            # lens, so drawing it costs light and buys nothing.
            return MAP_GROUND_LEVEL * 17
        # Buildings, roads, then labels, in the order the source already puts
        # them — roads ARE drawn brighter than buildings in this style, so the
        # curve must not reorder them, only spread them.
        #
        # The spread is by value under a gamma, not by rank. Ranking was tried
        # and inverted the picture: rank is share-of-pixels, buildings cover
        # the most pixels of any ink on a city tile, so they ranked highest
        # and came out BRIGHTER than the roads crossing them. Gamma lifts the
        # dim end without touching the ordering.
        normalized = ((min(high, value) - ground) / span) ** _TONE_GAMMA
        level = MAP_TONE_FLOOR + normalized * (MAP_TONE_CEILING - MAP_TONE_FLOOR)
        return int(round(level)) * 17

    return image.point(curve)


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
