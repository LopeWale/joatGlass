"""Mapbox Search Box + Directions adapter behind NavigationProvider.

The access token stays on the Mac. It is never put on the Hub wire, HUD, or
log messages. Search results remain temporary and are not persisted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Literal, cast
import urllib.error
import urllib.parse
import urllib.request

from ..core.interfaces import NavigationProvider
from ..core.navigation import (
    NavigationCandidate,
    NavigationMode,
    NavigationPoint,
    NavigationPolicies,
    ProviderRoute,
)
from ..core.protocol import (
    NavigationCoordinate,
    NavigationRoutePlan,
    NavigationRouteStep,
)

SEARCH_URL = "https://api.mapbox.com/search/searchbox/v1/forward"
DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox"
# The wire caps the polyline at 4096 points. A long walk legitimately
# returns more, so the geometry is DOWNSAMPLED to fit rather than thrown
# away — corridor tracking needs shape, not every vertex. Steps have no such
# lossless reduction, so an over-long route is reported as too_long.
MAX_ROUTE_COORDINATES = 4096
MAX_ROUTE_STEPS = 512

JsonObject = dict[str, object]
HttpJsonGet = Callable[[str, Mapping[str, str]], Awaitable[JsonObject]]
NavigationManeuver = Literal[
    "depart", "continue", "turn_left", "turn_right", "slight_left",
    "slight_right", "u_turn", "arrive",
]

_EARTH_RADIUS_METERS = 6_371_008.8
_SEARCH_STOP_WORDS = frozenset({"a", "an", "the"})


@dataclass(frozen=True, slots=True)
class _SearchCandidate:
    candidate: NavigationCandidate
    feature_type: str
    names: tuple[str, ...]
    categories: tuple[str, ...]
    source_index: int


# Why a route request failed, in terms the wearer can act on. "Route
# unavailable" for every cause taught the wearer nothing: a dead network, a
# destination with no walking path, and a route too long for the HUD each
# need a different next move.
RouteFailureReason = Literal["no_route", "too_long", "incomplete", "unreachable"]


class MapboxNavigationError(RuntimeError):
    """A bounded, token-free provider failure safe to surface internally."""

    def __init__(self, message: str, reason: RouteFailureReason = "unreachable") -> None:
        super().__init__(message)
        self.reason: RouteFailureReason = reason


class MapboxNavigationProvider(NavigationProvider):
    def __init__(
        self,
        access_token: str,
        policies: NavigationPolicies,
        *,
        http_get: HttpJsonGet | None = None,
    ) -> None:
        if not access_token.strip():
            raise ValueError("Mapbox access token is required")
        self._access_token = access_token
        self._policies = policies
        self._http_get = http_get or _stdlib_json_get

    async def search(
        self,
        query: str,
        proximity: NavigationPoint | None,
    ) -> list[NavigationCandidate]:
        bounded = query.strip()
        if not bounded or len(bounded) > 256:
            return []
        params = {
            "q": bounded,
            "access_token": self._access_token,
            "language": "en",
            # Proximity is a bias, not an ordering guarantee. Ask for enough
            # candidates to rank exact brands/categories by measured distance
            # on this Mac before the HUD sees them.
            "limit": "10",
            "types": "address,poi,place,street",
        }
        if proximity is not None:
            params["proximity"] = f"{proximity.longitude},{proximity.latitude}"
        payload = await self._http_get(SEARCH_URL, params)
        features = _objects(payload.get("features"))
        parsed = [
            candidate
            for index, feature in enumerate(features)
            if (candidate := _candidate(feature, proximity, index)) is not None
        ]
        ranked = _ranked_search_candidates(bounded, parsed, proximity)
        candidates = [item.candidate for item in ranked]
        if len(candidates) < 2:
            return candidates
        return [
            NavigationCandidate(
                choice_id=item.choice_id,
                label=item.label,
                address=item.address,
                point=item.point,
                repair_hint="street, city, or postcode",
                distance_m=item.distance_m,
            )
            for item in candidates
        ]

    async def route(
        self,
        origin: NavigationPoint,
        destination: NavigationCandidate,
        mode: NavigationMode,
    ) -> ProviderRoute:
        profile = "walking" if mode == "walking" else "cycling"
        coordinates = (
            f"{origin.longitude},{origin.latitude};"
            f"{destination.point.longitude},{destination.point.latitude}"
        )
        payload = await self._http_get(
            f"{DIRECTIONS_URL}/{profile}/{coordinates}",
            {
                "access_token": self._access_token,
                "steps": "true",
                "geometries": "geojson",
                "overview": "full",
                "language": "en",
                "banner_instructions": "true",
                "roundabout_exits": "true",
            },
        )
        routes = _objects(payload.get("routes"))
        if not routes:
            raise MapboxNavigationError(
                f"Mapbox returned no {profile} route", "no_route",
            )
        selected = routes[0]
        duration_s = _number(selected.get("duration"))
        geometry = _route_geometry(selected)
        steps = _route_steps(selected)
        if steps and len(steps) > MAX_ROUTE_STEPS:
            # Turn-by-turn beyond this is not a HUD experience; say so
            # instead of failing as though the map service were broken.
            raise MapboxNavigationError(
                "Route has more steps than the HUD can carry", "too_long",
            )
        if duration_s is None or duration_s < 0 or not geometry or not steps:
            raise MapboxNavigationError(
                "Mapbox returned an incomplete route", "incomplete",
            )
        digest = hashlib.sha256(
            json.dumps(
                [[point.latitude, point.longitude] for point in geometry],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        return ProviderRoute(
            route_id=f"mbx-{digest}",
            eta_minutes=duration_s / 60,
            plan=NavigationRoutePlan(
                mode=mode,
                geometry=geometry,
                steps=steps,
                policy=self._policies.for_mode(mode),
            ),
        )


async def _stdlib_json_get(url: str, params: Mapping[str, str]) -> JsonObject:
    return await asyncio.to_thread(_blocking_json_get, url, params)


def _blocking_json_get(url: str, params: Mapping[str, str]) -> JsonObject:
    request_url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        request_url,
        headers={"Accept": "application/json", "User-Agent": "JOAT-G2/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            value: object = json.load(response)
    except urllib.error.HTTPError as exc:
        raise MapboxNavigationError(
            f"Mapbox HTTP failure ({exc.code})", "unreachable",
        ) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise MapboxNavigationError("Mapbox request failed", "unreachable") from None
    if not isinstance(value, dict):
        raise MapboxNavigationError("Mapbox returned invalid JSON", "incomplete")
    return cast(JsonObject, value)


def _candidate(
    feature: JsonObject,
    proximity: NavigationPoint | None,
    source_index: int,
) -> _SearchCandidate | None:
    properties = _object(feature.get("properties"))
    geometry = _object(feature.get("geometry"))
    if properties is None or geometry is None:
        return None
    label = _text(properties.get("name_preferred")) or _text(properties.get("name"))
    provider_id = _text(properties.get("mapbox_id"))
    address = (
        _text(properties.get("full_address"))
        or _text(properties.get("place_formatted"))
        or _text(properties.get("address"))
    )
    point = _routable_point(properties) or _coordinate_pair(geometry.get("coordinates"))
    if label is None or provider_id is None or address is None or point is None:
        return None
    provider_distance = _number(properties.get("distance"))
    if provider_distance is None or provider_distance < 0:
        provider_distance = (
            _surface_distance_meters(proximity, point)
            if proximity is not None
            else None
        )
    choice_digest = hashlib.sha256(provider_id.encode("utf-8")).hexdigest()[:20]
    brands = _texts(properties.get("brand"))
    categories = (
        *_texts(properties.get("poi_category")),
        *_texts(properties.get("poi_category_ids")),
    )
    return _SearchCandidate(
        candidate=NavigationCandidate(
            choice_id=f"mbx-{choice_digest}",
            label=label[:96],
            address=address[:128],
            point=point,
            distance_m=provider_distance,
        ),
        feature_type=(_text(properties.get("feature_type")) or "").casefold(),
        names=(label, *brands),
        categories=categories,
        source_index=source_index,
    )


def _ranked_search_candidates(
    query: str,
    candidates: list[_SearchCandidate],
    proximity: NavigationPoint | None,
) -> list[_SearchCandidate]:
    """Prefer semantic POI matches, then measured proximity.

    Mapbox documents ``proximity`` as a result bias. It does not promise that
    the first feature is the closest, so provider order alone cannot answer a
    spoken "nearest" request. The relevance tier protects address and city
    searches while keeping an exact brand/category ahead of a closer street
    that merely contains the same words.
    """
    if not candidates:
        return []
    ranked = [(candidate, _search_match_rank(query, candidate)) for candidate in candidates]
    best_rank = min(rank for _, rank in ranked)
    if best_rank <= 1:
        # Once Mapbox returned a matching POI or brand/category, non-POI
        # address/place results are disambiguation noise rather than choices.
        ranked = [(candidate, rank) for candidate, rank in ranked if rank <= 1]
    elif best_rank <= 3:
        ranked = [(candidate, rank) for candidate, rank in ranked if rank <= 3]
    else:
        # Preserve Mapbox's fuzzy relevance fallback when none of the returned
        # metadata can substantiate a lexical match (misspellings, aliases).
        return candidates

    def key(value: tuple[_SearchCandidate, int]) -> tuple[float, ...]:
        candidate, rank = value
        if proximity is None or candidate.candidate.distance_m is None:
            return (float(rank), float(candidate.source_index))
        return (
            float(rank),
            candidate.candidate.distance_m,
            float(candidate.source_index),
        )

    ranked.sort(key=key)
    return [candidate for candidate, _ in ranked]


def _search_match_rank(query: str, result: _SearchCandidate) -> int:
    query_text = _search_text(query)
    query_tokens = _search_tokens(query)
    names = tuple(_search_text(value) for value in result.names)
    categories = tuple(_search_text(value) for value in result.categories)
    address = _search_text(result.candidate.address)
    is_poi = result.feature_type == "poi" or len(result.names) > 1 or bool(categories)

    if is_poi and query_text in (*names, *categories):
        return 0
    poi_text = " ".join((*names, *categories))
    if is_poi and query_tokens and query_tokens.issubset(_search_tokens(poi_text)):
        return 1
    if query_text in (*names, address):
        return 2
    ordinary_text = " ".join((*names, address))
    if query_tokens and query_tokens.issubset(_search_tokens(ordinary_text)):
        return 3
    return 4 if is_poi else 5


def _search_text(value: str) -> str:
    return re.sub(r"[\W_]+", " ", value.casefold(), flags=re.UNICODE).strip()


def _search_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token for token in _search_text(value).split()
        if token not in _SEARCH_STOP_WORDS
    )


def _surface_distance_meters(origin: NavigationPoint, target: NavigationPoint) -> float:
    """Deterministic great-circle fallback when Search Box omits distance."""
    origin_lat = math.radians(origin.latitude)
    target_lat = math.radians(target.latitude)
    delta_lat = target_lat - origin_lat
    delta_lon = math.radians(target.longitude - origin.longitude)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(origin_lat) * math.cos(target_lat) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_METERS * math.asin(min(1.0, math.sqrt(haversine)))


def _routable_point(properties: JsonObject) -> NavigationPoint | None:
    coordinates = _object(properties.get("coordinates"))
    if coordinates is None:
        return None
    for candidate in _objects(coordinates.get("routable_points")):
        latitude = _number(candidate.get("latitude"))
        longitude = _number(candidate.get("longitude"))
        if _valid_coordinates(latitude, longitude):
            return NavigationPoint(cast(float, latitude), cast(float, longitude))
    return None


def _route_geometry(route: JsonObject) -> list[NavigationCoordinate]:
    geometry = _object(route.get("geometry"))
    if geometry is None or geometry.get("type") != "LineString":
        return []
    raw = geometry.get("coordinates")
    if not isinstance(raw, list) or len(raw) < 2:
        return []
    if len(raw) > MAX_ROUTE_COORDINATES:
        raw = _downsampled(raw, MAX_ROUTE_COORDINATES)
    points: list[NavigationCoordinate] = []
    for pair in raw:
        point = _coordinate_pair(pair)
        if point is None:
            return []
        points.append(NavigationCoordinate(
            latitude=point.latitude,
            longitude=point.longitude,
        ))
    return points


def _downsampled(points: list[object], limit: int) -> list[object]:
    """Uniformly thin a polyline to `limit` points, keeping both endpoints.

    The corridor tracker measures distance to the line, so an evenly spaced
    subset preserves the route's shape; the exact vertex count does not
    matter. Dropping the route entirely (the old behaviour) made every walk
    longer than a few kilometres fail as "Route unavailable".
    """
    if limit < 2 or len(points) <= limit:
        return points
    last = len(points) - 1
    stride = last / (limit - 1)
    thinned = [points[min(last, round(index * stride))] for index in range(limit - 1)]
    thinned.append(points[last])
    return thinned


def _route_steps(route: JsonObject) -> list[NavigationRouteStep]:
    steps: list[NavigationRouteStep] = []
    cumulative_m = 0.0
    for leg in _objects(route.get("legs")):
        for raw_step in _objects(leg.get("steps")):
            maneuver = _object(raw_step.get("maneuver"))
            distance = _number(raw_step.get("distance"))
            if maneuver is None or distance is None or distance < 0:
                return []
            instruction = _text(maneuver.get("instruction")) or "Continue"
            steps.append(NavigationRouteStep(
                id=f"step-{len(steps) + 1}",
                maneuver=_maneuver(maneuver),
                instruction=instruction[:128],
                distance_along_route_m=cumulative_m,
            ))
            cumulative_m += distance
    if not steps:
        return []
    if steps[-1].maneuver != "arrive":
        steps.append(NavigationRouteStep(
            id=f"step-{len(steps) + 1}",
            maneuver="arrive",
            instruction="You have arrived at your destination",
            distance_along_route_m=cumulative_m,
        ))
    return steps


def _maneuver(value: JsonObject) -> NavigationManeuver:
    kind = _text(value.get("type"))
    modifier = _text(value.get("modifier"))
    if kind == "arrive":
        return "arrive"
    if kind == "depart":
        return "depart"
    if modifier == "uturn":
        return "u_turn"
    if modifier in ("left", "sharp left"):
        return "turn_left"
    if modifier in ("right", "sharp right"):
        return "turn_right"
    if modifier == "slight left":
        return "slight_left"
    if modifier == "slight right":
        return "slight_right"
    return "continue"


def _coordinate_pair(value: object) -> NavigationPoint | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    longitude = _number(value[0])
    latitude = _number(value[1])
    if not _valid_coordinates(latitude, longitude):
        return None
    return NavigationPoint(cast(float, latitude), cast(float, longitude))


def _valid_coordinates(latitude: float | None, longitude: float | None) -> bool:
    return (
        latitude is not None
        and longitude is not None
        and math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def _objects(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [cast(JsonObject, item) for item in value if isinstance(item, dict)]


def _object(value: object) -> JsonObject | None:
    return cast(JsonObject, value) if isinstance(value, dict) else None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := _text(item)) is not None)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
