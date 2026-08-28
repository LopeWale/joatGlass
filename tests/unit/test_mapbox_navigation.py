from __future__ import annotations

from collections.abc import Mapping

import pytest

from joat.core.navigation import NavigationCandidate, NavigationPoint, NavigationPolicies
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
