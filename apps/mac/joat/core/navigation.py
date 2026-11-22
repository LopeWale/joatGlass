"""Provider-neutral navigation domain objects.

Provider adapters may change; the destination-confirmation and route-install
contract does not. Spoken query text is deliberately absent from these durable
objects: only provider-canonical candidates cross the HUD boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .protocol import NavigationProgressPolicy, NavigationRoutePlan

NavigationMode = Literal["walking", "cycling", "driving"]


@dataclass(frozen=True, slots=True)
class NavigationPoint:
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class NavigationCandidate:
    choice_id: str
    label: str
    address: str
    point: NavigationPoint
    repair_hint: str | None = None
    # Provider-reported distance from the search proximity, in metres. Search
    # adapters may compute the same straight-line value when the provider does
    # not return it. It is temporary presentation/ranking data, not route
    # distance and never substitutes for Directions.
    distance_m: float | None = None


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    route_id: str
    eta_minutes: float
    plan: NavigationRoutePlan


def _default_driving_policy() -> NavigationProgressPolicy:
    return NavigationProgressPolicy(
        max_accuracy_m=45,
        stale_after_ms=6_000,
        corridor_radius_m=50,
        ambiguity_margin_m=10,
        ambiguity_progress_delta_m=180,
        search_back_m=220,
        search_forward_m=1_500,
        min_course_displacement_m=12,
        wrong_way_angle_deg=130,
        wrong_way_min_backtrack_m=45,
        wrong_way_samples=3,
        off_route_samples=3,
        arrival_radius_m=40,
        arrival_samples=2,
        u_turn_suppression_m=90,
    )


@dataclass(frozen=True, slots=True)
class NavigationPolicies:
    """Explicit pre-field candidate policy per supported travel mode."""

    walking: NavigationProgressPolicy
    cycling: NavigationProgressPolicy
    driving: NavigationProgressPolicy = field(default_factory=_default_driving_policy)

    def for_mode(self, mode: NavigationMode) -> NavigationProgressPolicy:
        if mode == "driving":
            return self.driving
        return self.walking if mode == "walking" else self.cycling
