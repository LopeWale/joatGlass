"""Compose tools that may use the wearer's phone location privately.

The assistant receives schemas before its first turn.  Location-dependent
tools therefore belong in its initial belt, rather than being registered once
the navigation subsystem happens to finish initializing.
"""

from __future__ import annotations

from .builtin_tools import CoordsProvider, RefreshCoordsProvider, weather_tool
from .nearby_places import NearbySearch, nearby_places_tool
from .tools import Tool


def wearer_location_tools(
    coords: CoordsProvider,
    refresh_coords: RefreshCoordsProvider,
    nearby_search: NearbySearch | None,
) -> list[Tool]:
    """Return the initial location-aware tool set for one assistant.

    ``nearby_search`` is present only when this Mac has its existing Mapbox
    provider configured.  Neither schema nor tool result contains coordinates:
    the provider receives them only inside the tool call.
    """

    tools = [weather_tool(coords, refresh_coords)]
    if nearby_search is not None:
        tools.append(nearby_places_tool(coords, refresh_coords, nearby_search))
    return tools
