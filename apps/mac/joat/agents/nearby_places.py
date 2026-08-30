"""Fresh-location nearby-place tool.

The model names a category; the Mac injects the wearer's current phone
coordinates into the provider call.  Coordinates never enter the prompt,
tool result, HUD, or logs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ..core.navigation import NavigationCandidate, NavigationPoint
from .tools import Tool

Coordinates = tuple[float, float]
CoordsProvider = Callable[[], Coordinates | None]
RefreshCoordsProvider = Callable[[], Awaitable[Coordinates | None]]
NearbySearch = Callable[[str, NavigationPoint], Awaitable[Sequence[NavigationCandidate]]]

_MAX_QUERY_CHARS = 256
_MAX_RESULTS = 3


def nearby_places_tool(
    coords: CoordsProvider,
    refresh_coords: RefreshCoordsProvider,
    search: NearbySearch,
) -> Tool:
    """Create a nearby-place tool bound to fresh phone location only."""

    async def run(args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "error: say what kind of place to find"
        if len(query) > _MAX_QUERY_CHARS:
            return "error: nearby-place query is too long"

        here = coords()
        if here is None:
            here = await refresh_coords()
        if here is None:
            return "error: phone location is unavailable or stale"

        results = list(await search(query, NavigationPoint(*here)))[:_MAX_RESULTS]
        if not results:
            return "no nearby places found"
        return "Nearby places: " + "; ".join(
            f"{candidate.label} — {candidate.address}" for candidate in results
        )

    return Tool(
        name="find_nearby_places",
        description=(
            "Find nearby restaurants, cafés, stores, or other place categories. "
            "Use this for questions like 'what is near me?' or 'restaurants nearby'. "
            "Provide the category in query; the wearer location is obtained privately."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Place category or search, e.g. 'restaurants' or 'coffee shops'.",
                },
            },
            "required": ["query"],
        },
        run=run,
    )
