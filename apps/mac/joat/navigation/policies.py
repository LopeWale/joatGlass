"""Explicit pre-field navigation policies used by recorded-route replay.

These values are release candidates, not vendor truths. They intentionally
live outside the tracker and provider so real packaged-G2 evidence can replace
them per mode without changing either algorithm.
"""

from __future__ import annotations

from ..core.navigation import NavigationPolicies
from ..core.protocol import NavigationProgressPolicy


def candidate_navigation_policies() -> NavigationPolicies:
    return NavigationPolicies(
        walking=NavigationProgressPolicy(
            max_accuracy_m=25,
            stale_after_ms=5_000,
            corridor_radius_m=25,
            ambiguity_margin_m=6,
            ambiguity_progress_delta_m=80,
            search_back_m=100,
            search_forward_m=400,
            min_course_displacement_m=5,
            wrong_way_angle_deg=120,
            wrong_way_min_backtrack_m=12,
            wrong_way_samples=3,
            off_route_samples=3,
            arrival_radius_m=15,
            arrival_samples=2,
            u_turn_suppression_m=40,
        ),
        cycling=NavigationProgressPolicy(
            max_accuracy_m=20,
            stale_after_ms=4_000,
            corridor_radius_m=35,
            ambiguity_margin_m=7,
            ambiguity_progress_delta_m=120,
            search_back_m=160,
            search_forward_m=800,
            min_course_displacement_m=8,
            wrong_way_angle_deg=125,
            wrong_way_min_backtrack_m=25,
            wrong_way_samples=3,
            off_route_samples=3,
            arrival_radius_m=20,
            arrival_samples=2,
            u_turn_suppression_m=60,
        ),
    )
