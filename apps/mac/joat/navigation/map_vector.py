"""The wearer's surroundings as ROAD DATA, not a picture of roads.

The first attempt at this fetched a Mapbox Static Image and pushed it
through a tone curve. That was the wrong primitive twice over: a raster
styled for a full-colour phone screen loses almost everything when it is
crushed to sixteen levels of green, and its street names are baked in at a
size and weight nobody chose for a 576x288 optical HUD — they were the
first thing to disappear.

Vector tiles give the actual geometry instead: every road as a polyline
with a class and, crucially, a NAME. JOAT then draws them itself, so line
weight, brightness, which roads appear at all, and how their names are set
are all decisions this codebase makes rather than inherits. A named street
is the difference between "a line is near you" and "you are on Shartel".

The Mapbox token stays on this Mac; the plugin receives coordinates.
"""

from __future__ import annotations

import gzip
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import asyncio
import logging

log = logging.getLogger(__name__)

VECTOR_TILE_URL = "https://api.mapbox.com/v4/mapbox.mapbox-streets-v8"
#: One tile at z15 spans roughly a kilometre here — comfortably more than
#: the panel shows, so a wearer walking never outruns the fetched tile
#: before the distance-throttled refresh catches up.
VECTOR_ZOOM = 15
#: Mapbox vector tiles use a 4096-unit local grid.
TILE_EXTENT = 4096

#: Which roads earn HUD pixels, and how loudly. Everything absent from this
#: map (service alleys, driveways, aerialways) is deliberately dropped: on
#: an optical display the wearer pays for every lit pixel, and a driveway
#: never helped anyone place themselves.
ROAD_WEIGHTS: dict[str, int] = {
    "motorway": 3,
    "motorway_link": 2,
    "trunk": 3,
    "trunk_link": 2,
    "primary": 3,
    "secondary": 2,
    "tertiary": 2,
    "street": 2,
    "street_limited": 2,
    "pedestrian": 1,
    "path": 1,
}

HttpGet = Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class Road:
    """One drawable road: its shape, how important it is, and its name."""

    name: str
    weight: int
    #: (latitude, longitude) pairs.
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class RoadNetwork:
    latitude: float
    longitude: float
    roads: tuple[Road, ...] = field(default_factory=tuple)


class MapVectorError(RuntimeError):
    """A bounded, token-free failure. Guidance continues without streets."""


def tile_for(latitude: float, longitude: float, zoom: int) -> tuple[int, int]:
    scale = 2 ** zoom
    latitude_rad = math.radians(latitude)
    return (
        int((longitude + 180.0) / 360.0 * scale),
        int(
            (
                1.0
                - math.log(math.tan(latitude_rad) + 1 / math.cos(latitude_rad))
                / math.pi
            )
            / 2.0
            * scale
        ),
    )


def _tile_point_to_lat_lon(
    x: float, y: float, tile_x: int, tile_y: int, zoom: int,
) -> tuple[float, float]:
    """Vector-tile local grid → WGS84.

    Tile y grows south while the local grid grows north, which is why the
    y term is subtracted rather than added; getting this backwards mirrors
    the whole city.
    """
    scale = 2 ** zoom
    world_x = tile_x + x / TILE_EXTENT
    world_y = tile_y + (1 - y / TILE_EXTENT)
    longitude = world_x / scale * 360.0 - 180.0
    latitude = math.degrees(
        math.atan(math.sinh(math.pi * (1 - 2 * world_y / scale)))
    )
    return latitude, longitude


async def fetch_road_network(
    access_token: str,
    latitude: float,
    longitude: float,
    *,
    zoom: int = VECTOR_ZOOM,
    max_roads: int = 96,
    http_get: HttpGet | None = None,
) -> RoadNetwork:
    """Fetch the road network around one point, ready to draw."""
    if not access_token.strip():
        raise MapVectorError("Mapbox access token is required")
    tile_x, tile_y = tile_for(latitude, longitude, zoom)
    fetch = http_get or _stdlib_get
    url = (
        f"{VECTOR_TILE_URL}/{zoom}/{tile_x}/{tile_y}.mvt"
        f"?access_token={urllib.parse.quote(access_token)}"
    )
    raw = await fetch(url)
    roads = await asyncio.to_thread(
        _decode_roads, raw, tile_x, tile_y, zoom, latitude, longitude, max_roads,
    )
    return RoadNetwork(latitude=latitude, longitude=longitude, roads=roads)


def _decode_roads(
    raw: bytes,
    tile_x: int,
    tile_y: int,
    zoom: int,
    latitude: float,
    longitude: float,
    max_roads: int,
) -> tuple[Road, ...]:
    try:
        # No stubs published; the decoded shape is validated below anyway.
        import mapbox_vector_tile  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise MapVectorError("mapbox-vector-tile is required") from exc
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        tile = mapbox_vector_tile.decode(raw)
    except Exception:
        raise MapVectorError("Mapbox returned an undecodable tile") from None

    candidates: list[tuple[float, Road]] = []
    for feature in tile.get("road", {}).get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") not in ("LineString", "MultiLineString"):
            continue
        properties = feature.get("properties") or {}
        weight = ROAD_WEIGHTS.get(str(properties.get("class", "")), 0)
        if weight == 0:
            continue
        name = str(properties.get("name") or "")
        lines = (
            [geometry["coordinates"]] if geometry["type"] == "LineString"
            else geometry["coordinates"]
        )
        for line in lines:
            points = tuple(
                _tile_point_to_lat_lon(px, py, tile_x, tile_y, zoom)
                for px, py in line
            )
            if len(points) < 2:
                continue
            # Nearest-first, so a bounded payload keeps the roads the wearer
            # can actually see rather than whichever the tile listed first.
            distance = min(
                (point[0] - latitude) ** 2 + (point[1] - longitude) ** 2
                for point in points
            )
            candidates.append((distance, Road(name=name, weight=weight, points=points)))

    candidates.sort(key=lambda item: item[0])
    return tuple(road for _distance, road in candidates[:max_roads])


async def _stdlib_get(url: str) -> bytes:
    return await asyncio.to_thread(_blocking_get, url)


def _blocking_get(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"Accept": "application/x-protobuf", "User-Agent": "JOAT-G2/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        # The URL carries the token; only the status code is safe to surface.
        raise MapVectorError(f"Mapbox tile HTTP failure ({exc.code})") from None
    except (urllib.error.URLError, TimeoutError):
        raise MapVectorError("Mapbox tile request failed") from None
