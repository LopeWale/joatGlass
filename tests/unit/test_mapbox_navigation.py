from __future__ import annotations

from collections.abc import Mapping

from PIL import Image
import pytest

from joat.core.navigation import NavigationCandidate, NavigationPoint, NavigationPolicies
from joat.navigation.map_image import (
    MAP_TONE_FLOOR,
    MAP_TONE_CEILING,
    _tone_curve,
)
from joat.core.protocol import NavigationProgressPolicy
from joat.navigation.mapbox import (
    DIRECTIONS_URL,
    SEARCH_URL,
    MapboxNavigationError,
    MapboxNavigationProvider,
)


def policy() -> NavigationProgressPolicy:
    return NavigationProgressPolicy(
        max_accuracy_m=20, stale_after_ms=5000, corridor_radius_m=30,
        ambiguity_margin_m=5, ambiguity_progress_delta_m=100,
        search_back_m=120, search_forward_m=500, min_course_displacement_m=5,
        wrong_way_angle_deg=120, wrong_way_min_backtrack_m=15,
        wrong_way_samples=2, off_route_samples=3, arrival_radius_m=15,
        arrival_samples=2, u_turn_suppression_m=50,
    )


class FakeHttp:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, params: Mapping[str, str]) -> dict[str, object]:
        self.calls.append((url, dict(params)))
        return self.responses.pop(0)


def provider(http: FakeHttp) -> MapboxNavigationProvider:
    return MapboxNavigationProvider(
        "secret-token",
        NavigationPolicies(walking=policy(), cycling=policy()),
        http_get=http,
    )


class TestMapboxNavigationProvider:
    async def test_search_returns_canonical_poi_and_routable_point(self) -> None:
        http = FakeHttp([{"features": [{
            "geometry": {"type": "Point", "coordinates": [-87.64, 41.91]},
            "properties": {
                "mapbox_id": "poi.cajun-corner",
                "name": "Cajun Corner",
                "full_address": "2301 N Lincoln Ave, Chicago, Illinois 60614",
                "coordinates": {"routable_points": [{
                    "latitude": 41.9101, "longitude": -87.6401,
                }]},
            },
        }]}])

        results = await provider(http).search(
            "Cajun Corner", NavigationPoint(latitude=41.9, longitude=-87.65),
        )

        assert len(results) == 1
        assert results[0].label == "Cajun Corner"
        assert results[0].address.startswith("2301 N Lincoln")
        assert results[0].point == NavigationPoint(41.9101, -87.6401)
        assert results[0].choice_id.startswith("mbx-")
        url, params = http.calls[0]
        assert url == SEARCH_URL
        assert params["proximity"] == "-87.65,41.9"
        assert params["types"] == "address,poi,place,street"
        assert "secret-token" not in repr(results)

    async def test_multiple_results_nominate_component_repair_without_auto_approval(self) -> None:
        features = []
        for identifier, address, longitude in (
            ("west", "100 W Main St, Chicago", -87.7),
            ("east", "100 E Main St, Chicago", -87.6),
        ):
            features.append({
                "geometry": {"type": "Point", "coordinates": [longitude, 41.9]},
                "properties": {
                    "mapbox_id": identifier, "name": "Cajun Corner",
                    "full_address": address,
                },
            })
        results = await provider(FakeHttp([{"features": features}])).search(
            "Cajun Corner", None,
        )
        assert len(results) == 2
        assert {item.repair_hint for item in results} == {"street, city, or postcode"}

    async def test_walking_route_parses_geometry_and_cumulative_maneuvers(self) -> None:
        http = FakeHttp([{"routes": [{
            "duration": 600,
            "geometry": {"type": "LineString", "coordinates": [
                [-87.65, 41.9], [-87.645, 41.9], [-87.64, 41.91],
            ]},
            "legs": [{"steps": [
                {"distance": 60, "maneuver": {
                    "type": "depart", "instruction": "Head east on Test Street",
                }},
                {"distance": 40, "maneuver": {
                    "type": "turn", "modifier": "right",
                    "instruction": "Turn right onto Lincoln Avenue",
                }},
                {"distance": 0, "maneuver": {
                    "type": "arrive", "instruction": "You have arrived",
                }},
            ]}],
        }]}])
        destination = NavigationCandidate(
            choice_id="place", label="Cajun Corner", address="2301 N Lincoln",
            point=NavigationPoint(41.91, -87.64),
        )

        route = await provider(http).route(
            NavigationPoint(41.9, -87.65), destination, "walking",
        )

        assert route.eta_minutes == 10
        assert route.plan.mode == "walking"
        assert [step.maneuver for step in route.plan.steps] == [
            "depart", "turn_right", "arrive",
        ]
        assert [step.distance_along_route_m for step in route.plan.steps] == [0, 60, 100]
        assert len(route.plan.geometry) == 3
        url, params = http.calls[0]
        assert url.startswith(f"{DIRECTIONS_URL}/walking/")
        assert params["steps"] == "true"
        assert params["overview"] == "full"

    async def test_incomplete_route_fails_closed(self) -> None:
        with pytest.raises(MapboxNavigationError, match="incomplete"):
            await provider(FakeHttp([{"routes": [{"duration": 5}]}])).route(
                NavigationPoint(1, 1),
                NavigationCandidate("place", "Place", "1 Main", NavigationPoint(2, 2)),
                "cycling",
            )


def _long_route(points: int, steps: int) -> dict[str, object]:
    """A Mapbox route payload of arbitrary size, as a real long walk returns."""
    return {"routes": [{
        "duration": 4200.0,
        "geometry": {
            "type": "LineString",
            "coordinates": [[-87.64 + index * 1e-5, 41.91] for index in range(points)],
        },
        "legs": [{"steps": [
            {"distance": 12.0, "maneuver": {"type": "turn", "modifier": "left",
                                            "instruction": f"Turn left {index}"}}
            for index in range(steps)
        ]}],
    }]}


class TestToneCurve:
    def test_uniform_dark_tone_stays_black_instead_of_bleeding_to_white(self) -> None:
        image = Image.new("L", (10, 10), color=200)

        toned = _tone_curve(image)

        assert list(toned.getdata()) == [0] * 100

    def test_bare_land_is_unlit_and_the_ink_is_ranked_above_it(self) -> None:
        """Land dark, everything drawn on it separated across the band.

        Lighting the ground was tried, to give blocks "area", and it cost
        the one thing the wearer reads most: Mapbox draws street NAMES as
        the brightest ink in the tile, so on a lit ground they invert into
        dark glyphs on a bright field — the lowest-contrast pairing this
        panel can produce — and the names became unreadable.
        """
        image = Image.new("L", (10, 10), color=20)  # bare land: the mode
        for x in range(3):  # buildings: a little above the land
            image.putpixel((x, 0), 90)
        for x in range(3, 6):  # roads
            image.putpixel((x, 0), 160)
        for x in range(6, 9):  # labels: the brightest ink
            image.putpixel((x, 0), 240)

        levels = [value // 17 for value in _tone_curve(image).getdata()]
        building, road, label, land = levels[0], levels[3], levels[6], levels[9]

        assert land == 0, "bare land must stay clear glass"
        assert building < road < label, "the ink must land on separate steps"
        assert building >= MAP_TONE_FLOOR, "drawn shapes must stay lit"
        assert label <= MAP_TONE_CEILING, "the map must not spend JOAT's levels"

    def test_map_never_spends_the_levels_reserved_for_joats_own_marks(self) -> None:
        """The route has to be able to out-shine any road it crosses.

        This cannot be checked from a simulator screenshot — that capture
        path returns the same value for level 11 and level 14 — so the
        headroom is pinned here, in the data, where it is real.
        """
        image = Image.new("L", (16, 16), color=0)
        for index, value in enumerate(range(0, 256, 16)):  # a full ramp
            image.putpixel((index, 0), value)

        toned = _tone_curve(image)

        assert max(toned.getdata()) // 17 <= MAP_TONE_CEILING


class TestLongRouteHandling:
    """A real walk returns far more polyline vertices than the wire allows.
    Throwing the route away made every longer route read "Route unavailable"."""

    async def test_oversized_geometry_is_downsampled_instead_of_rejected(self) -> None:
        http = FakeHttp([_long_route(9_000, 40)])
        route = await provider(http).route(
            NavigationPoint(41.90, -87.63),
            NavigationCandidate(
                choice_id="mbx-1", label="Somewhere far",
                address="Far away", point=NavigationPoint(41.99, -87.50),
            ),
            "walking",
        )
        assert len(route.plan.geometry) == 4_096
        # Both endpoints survive: the corridor must still start and end right.
        assert route.plan.geometry[0].longitude == pytest.approx(-87.64)
        assert route.plan.geometry[-1].longitude == pytest.approx(-87.64 + 8_999 * 1e-5)
        # Monotonic along the original polyline — shape preserved, not shuffled.
        longitudes = [point.longitude for point in route.plan.geometry]
        assert longitudes == sorted(longitudes)

    async def test_a_route_with_too_many_steps_says_so_precisely(self) -> None:
        http = FakeHttp([_long_route(100, 600)])
        with pytest.raises(MapboxNavigationError) as excinfo:
            await provider(http).route(
                NavigationPoint(41.90, -87.63),
                NavigationCandidate(
                    choice_id="mbx-1", label="Somewhere far",
                    address="Far away", point=NavigationPoint(41.99, -87.50),
                ),
                "walking",
            )
        assert excinfo.value.reason == "too_long"

    async def test_no_route_is_reported_as_its_own_reason(self) -> None:
        http = FakeHttp([{"routes": []}])
        with pytest.raises(MapboxNavigationError) as excinfo:
            await provider(http).route(
                NavigationPoint(41.90, -87.63),
                NavigationCandidate(
                    choice_id="mbx-1", label="Across water",
                    address="No path", point=NavigationPoint(41.99, -87.50),
                ),
                "walking",
            )
        assert excinfo.value.reason == "no_route"
