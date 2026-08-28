from joat.navigation.policies import candidate_navigation_policies


def test_walking_and_cycling_policies_are_explicit_and_distinct() -> None:
    policies = candidate_navigation_policies()
    assert policies.walking.corridor_radius_m < policies.cycling.corridor_radius_m
    assert policies.walking.min_course_displacement_m < (
        policies.cycling.min_course_displacement_m
    )
    assert policies.walking.wrong_way_min_backtrack_m < (
        policies.cycling.wrong_way_min_backtrack_m
    )
    assert policies.walking.off_route_samples == 3
    assert policies.cycling.off_route_samples == 3
