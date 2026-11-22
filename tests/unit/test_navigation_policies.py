from joat.navigation.policies import candidate_navigation_policies


def test_walking_cycling_and_driving_policies_are_explicit_and_distinct() -> None:
    policies = candidate_navigation_policies()
    assert policies.walking.corridor_radius_m < policies.cycling.corridor_radius_m < policies.driving.corridor_radius_m
    assert policies.walking.min_course_displacement_m < (
        policies.cycling.min_course_displacement_m
    ) < policies.driving.min_course_displacement_m
    assert policies.walking.wrong_way_min_backtrack_m < (
        policies.cycling.wrong_way_min_backtrack_m
    ) < policies.driving.wrong_way_min_backtrack_m
    assert policies.walking.off_route_samples == 3
    assert policies.cycling.off_route_samples == 3
    driving = policies.for_mode("driving")
    assert driving.corridor_radius_m == 50
    assert driving.arrival_radius_m == 40
    assert driving.max_accuracy_m == 45
    assert driving.stale_after_ms == 6_000
    assert driving.search_forward_m == 1_500
    assert driving.wrong_way_angle_deg == 130
    assert driving.u_turn_suppression_m == 90
