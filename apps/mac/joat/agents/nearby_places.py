"""Fresh-location nearby-place tool.

The model names a category; the Mac injects the wearer's current phone
coordinates into the provider call.  Coordinates never enter the prompt,
tool result, HUD, or logs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
import re
from typing import Any

from ..core.navigation import NavigationCandidate, NavigationPoint
from .tools import Tool

Coordinates = tuple[float, float]
CoordsProvider = Callable[[], Coordinates | None]
RefreshCoordsProvider = Callable[[], Awaitable[Coordinates | None]]
NearbySearch = Callable[[str, NavigationPoint], Awaitable[Sequence[NavigationCandidate]]]

_MAX_QUERY_CHARS = 256
_MAX_RESULTS = 3

_TRAILING_PUNCTUATION = "?!.,"
_LOCALITY_SUFFIX = re.compile(
    r"\s+(?:near(?:\s+to)?\s+(?:me|here|us)|around\s+(?:me|here|us)|close\s+by|nearby)$",
    re.IGNORECASE,
)
_LOCALITY_PREFIX = re.compile(r"^nearby\s+", re.IGNORECASE)
_REQUEST_PREFIX = re.compile(
    r"^(?:can you\s+|could you\s+|please\s+)*(?:"
    r"find|show(?: me)?|look for|what(?:'s| is| are)|where(?:'s| is)|"
    r"are there|is there|do you have)\s+",
    re.IGNORECASE,
)
_NEAREST_PREFIX = re.compile(r"^(?:the\s+)?(?:closest|nearest)\s+", re.IGNORECASE)
_BARE_LOCALITY = re.compile(
    r"^(?:what(?:'s| is)\s+)?(?:nearby|near(?:\s+to)?\s+(?:me|here|us)|"
    r"around\s+(?:me|here|us)|close\s+by)$",
    re.IGNORECASE,
)


def nearby_place_intent(utterance: str) -> dict[str, Any] | None:
    """Recognize only explicit local-place requests from wearer speech.

    The tool schema already lets the model select an arbitrary nearby lookup.
    This narrow resolver exists because the live small model reliably uses
    other tools but has never selected the advertised nearby one.  Returning
    an empty category for a bare "what is near me?" intentionally lets the
    existing tool return its truthful request for a category without fetching
    a location or calling the provider.
    """
    request = " ".join(utterance.strip().rstrip(_TRAILING_PUNCTUATION).split())
    if not request:
        return None
    if _BARE_LOCALITY.fullmatch(request):
        return {"query": ""}

    unprefixed = _REQUEST_PREFIX.sub("", request)
    category = _LOCALITY_PREFIX.sub("", unprefixed)
    category = _LOCALITY_SUFFIX.sub("", category)
    category = _NEAREST_PREFIX.sub("", category)
    category = " ".join(category.split())

    # A locality marker is mandatory.  Do not reinterpret general questions
    # about a place as a private location search.
    explicit_locality = (
        _LOCALITY_PREFIX.match(unprefixed) is not None
        or _LOCALITY_SUFFIX.search(unprefixed) is not None
        or _NEAREST_PREFIX.match(unprefixed) is not None
    )
    if not explicit_locality:
        return None
    return {"query": category}


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
        explicit_intent=nearby_place_intent,
        # The resolver owns the normal nearby phrases. Keep these hints as a
        # safe fallback if that convenience resolver ever declines or fails:
        # the model-directed path remains preferable to silently ignoring a
        # local-place request.
        decision_hints=("near", "nearby", "nearest", "closest", "close by"),
    )
