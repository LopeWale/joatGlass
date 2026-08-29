from __future__ import annotations

import asyncio

import pytest

from joat.core.interfaces import GlassesTransport, NavigationProvider
from joat.core.navigation import (
    NavigationCandidate,
    NavigationMode,
    NavigationPoint,
    ProviderRoute,
)
from joat.core.protocol import (
    DownlinkMessage,
    NavigationAction,
    NavigationCoordinate,
    NavigationHudView,
    NavigationOrigin,
    NavigationOriginFailure,
    NavigationOriginRequest,
    NavigationProgressPolicy,
    NavigationRerouteRequest,
    NavigationRoute,
    NavigationRouteAck,
    NavigationRoutePlan,
    NavigationRouteStep,
    NavigationState,
    NavigationStop,
    NavigationView,
    NavigationViewAck,
)
from joat.services.navigation import NavigationSession
from joat.services.navigation import NavigationUnavailableSession, navigation_query


class SinkTransport(GlassesTransport):
    def __init__(self) -> None:
        self.sent: list[DownlinkMessage] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, message: DownlinkMessage) -> None:
        message.to_wire()
        self.sent.append(message)

    @property
    def connected(self) -> bool:
        return True

    def on_audio(self, callback: object) -> None: ...
    def on_input(self, callback: object) -> None: ...
    def on_device(self, callback: object) -> None: ...


def plan() -> NavigationRoutePlan:
    return NavigationRoutePlan(
        mode="walking",
        geometry=[
            NavigationCoordinate(latitude=41.9, longitude=-87.65),
            NavigationCoordinate(latitude=41.901, longitude=-87.64),
        ],
        steps=[NavigationRouteStep(
            id="step-1", maneuver="turn_right", instruction="onto Test Ave",
            distance_along_route_m=500,
        )],
        policy=NavigationProgressPolicy(
            max_accuracy_m=20, stale_after_ms=5000, corridor_radius_m=30,
            ambiguity_margin_m=5, ambiguity_progress_delta_m=100,
            search_back_m=120, search_forward_m=500, min_course_displacement_m=5,
            wrong_way_angle_deg=120, wrong_way_min_backtrack_m=15,
            wrong_way_samples=2, off_route_samples=3, arrival_radius_m=15,
            arrival_samples=2, u_turn_suppression_m=50,
        ),
    )


class FakeProvider(NavigationProvider):
    def __init__(self, candidates: list[NavigationCandidate]) -> None:
        self.candidates = candidates
        self.searches: list[str] = []
        self.routes: list[tuple[NavigationPoint, NavigationCandidate, str]] = []

    async def search(
        self, query: str, proximity: NavigationPoint | None,
    ) -> list[NavigationCandidate]:
        self.searches.append(query)
        return self.candidates

    async def route(
        self, origin: NavigationPoint, destination: NavigationCandidate, mode: NavigationMode,
    ) -> ProviderRoute:
        self.routes.append((origin, destination, mode))
        return ProviderRoute(route_id="route-1", eta_minutes=18, plan=plan())


class CancellationResistantProvider(FakeProvider):
    """Simulate an HTTP client that completes after task cancellation."""

    def __init__(self, candidates: list[NavigationCandidate]) -> None:
        super().__init__(candidates)
        self.reroute_started = asyncio.Event()
        self.release_reroute = asyncio.Event()

    async def route(
        self, origin: NavigationPoint, destination: NavigationCandidate,
        mode: NavigationMode,
    ) -> ProviderRoute:
        self.routes.append((origin, destination, mode))
        if len(self.routes) == 1:
            return ProviderRoute(route_id="route-1", eta_minutes=18, plan=plan())
        self.reroute_started.set()
        try:
            await self.release_reroute.wait()
        except asyncio.CancelledError:
            await self.release_reroute.wait()
        return ProviderRoute(route_id="stale-reroute", eta_minutes=21, plan=plan())


class InvalidRouteProvider(FakeProvider):
    async def route(
        self, origin: NavigationPoint, destination: NavigationCandidate,
        mode: NavigationMode,
    ) -> ProviderRoute:
        self.routes.append((origin, destination, mode))
        invalid = NavigationRoutePlan(
            mode="cycling",
            geometry=[],
            steps=[],
            policy=plan().policy,
        )
        return ProviderRoute(route_id="wrong-mode", eta_minutes=18, plan=invalid)


class WireInvalidRouteProvider(FakeProvider):
    def __init__(
        self, candidates: list[NavigationCandidate], *, unordered_steps: bool,
    ) -> None:
        super().__init__(candidates)
        self.unordered_steps = unordered_steps

    async def route(
        self, origin: NavigationPoint, destination: NavigationCandidate,
        mode: NavigationMode,
    ) -> ProviderRoute:
        selected = plan()
        if self.unordered_steps:
            selected = NavigationRoutePlan(
                mode="walking",
                geometry=selected.geometry,
                steps=[
                    NavigationRouteStep(
                        id="later", maneuver="turn_right", instruction="later",
                        distance_along_route_m=400,
                    ),
                    NavigationRouteStep(
                        id="earlier", maneuver="continue", instruction="earlier",
                        distance_along_route_m=200,
                    ),
                ],
                policy=selected.policy,
            )
        return ProviderRoute(
            route_id="valid" if self.unordered_steps else "x" * 129,
            eta_minutes=18,
            plan=selected,
        )


async def wait_for(
    transport: SinkTransport,
    kind: type[object],
    *,
    count: int = 1,
) -> object:
    for _ in range(100):
        found = [message for message in transport.sent if isinstance(message, kind)]
        if len(found) >= count:
            return found[-1]
        await asyncio.sleep(0)
    raise AssertionError(f"{kind.__name__} was not sent")


def action(view: NavigationView, name: str, choice_id: str | None = None) -> NavigationAction:
    return NavigationAction(
        session_id=view.session_id,
        request_id=view.request_id,
        generation=view.generation,
        action=name,
        choice_id=choice_id,
    )


class TestNavigationSession:
    def test_requires_an_explicit_navigation_invocation(self) -> None:
        assert navigation_query("navigate to Cajun Corner") == "Cajun Corner"
        assert navigation_query("Directions to 2301 N Lincoln Ave?") == (
            "2301 N Lincoln Ave"
        )
        assert navigation_query("take me to the lake") == "the lake"
        assert navigation_query("Cajun Corner") is None

    async def test_missing_provider_is_visible_and_closes_on_bound_tap(self) -> None:
        transport = SinkTransport()
        session = NavigationUnavailableSession(transport, session_id="nav-offline")
        task = asyncio.create_task(session.run())
        view = await wait_for(transport, NavigationView)
        assert isinstance(view, NavigationView)
        assert view.navigation.view == "error"
        assert "not configured" in view.navigation.secondary
        session.accept_action(action(view, "retry"))
        await task
        assert isinstance(transport.sent[-1], NavigationStop)

    async def test_rejected_unavailable_view_closes_without_waiting_for_a_gesture(self) -> None:
        transport = SinkTransport()
        session = NavigationUnavailableSession(transport, session_id="nav-offline")
        task = asyncio.create_task(session.run())
        view = await wait_for(transport, NavigationView)
        assert isinstance(view, NavigationView)

        session.accept_view_ack(NavigationViewAck(
            session_id=view.session_id, request_id=view.request_id,
            generation=view.generation, outcome="rejected",
        ))
        await task
        assert isinstance(transport.sent[-1], NavigationStop)

    async def test_origin_failure_returns_a_visible_recoverable_error(self) -> None:
        candidate = NavigationCandidate(
            choice_id="place-1", label="Cajun Corner",
            address="2301 N Lincoln Ave, Chicago",
            point=NavigationPoint(latitude=41.92, longitude=-87.65),
        )
        transport = SinkTransport()
        session = NavigationSession(
            "Cajun Corner", FakeProvider([candidate]), transport,
            session_id="nav-origin-failure",
        )
        task = asyncio.create_task(session.run())
        destination = await wait_for(transport, NavigationView)
        assert isinstance(destination, NavigationView)
        session.accept_action(action(destination, "confirm_destination"))
        mode = await wait_for(transport, NavigationView, count=2)
        assert isinstance(mode, NavigationView)
        session.accept_action(action(mode, "select_mode", "walking"))
        origin = await wait_for(transport, NavigationOriginRequest)
        assert isinstance(origin, NavigationOriginRequest)

        session.accept_origin_failure(NavigationOriginFailure(
            session_id=origin.session_id, request_id=origin.request_id,
            generation=origin.generation, reason="unavailable",
        ))
        error = await wait_for(transport, NavigationView, count=4)
        assert isinstance(error, NavigationView)
        assert error.navigation.primary == "Location unavailable"
        assert "foreground" in error.navigation.secondary
        session.accept_action(action(error, "retry"))
        retried_origin = await wait_for(transport, NavigationOriginRequest, count=2)
        assert isinstance(retried_origin, NavigationOriginRequest)
        assert retried_origin.request_id != origin.request_id
        session.cancel()
        await task

    async def test_left_temple_cancel_is_honored_while_location_is_pending(self) -> None:
        candidate = NavigationCandidate(
            choice_id="place-1", label="Cajun Corner",
            address="2301 N Lincoln Ave, Chicago",
            point=NavigationPoint(latitude=41.92, longitude=-87.65),
        )
        transport = SinkTransport()
        session = NavigationSession(
            "Cajun Corner", FakeProvider([candidate]), transport,
            session_id="nav-cancel-locating",
        )
        task = asyncio.create_task(session.run())

        destination = await wait_for(transport, NavigationView)
        assert isinstance(destination, NavigationView)
        session.accept_action(action(destination, "confirm_destination"))
        mode = await wait_for(transport, NavigationView, count=2)
        assert isinstance(mode, NavigationView)
        session.accept_action(action(mode, "select_mode", "walking"))
        locating = await wait_for(transport, NavigationView, count=3)
        assert isinstance(locating, NavigationView)
        assert locating.navigation.interaction == "none"

        session.accept_action(action(locating, "cancel"))
        await task
        assert isinstance(transport.sent[-1], NavigationStop)

    async def test_provider_route_must_match_the_selected_mode_and_have_steps(self) -> None:
        candidate = NavigationCandidate(
            choice_id="place-1", label="Place", address="1 Main St",
            point=NavigationPoint(1, 1),
        )
        transport = SinkTransport()
        session = NavigationSession(
            "Place", InvalidRouteProvider([candidate]), transport,
            session_id="nav-invalid-route",
        )
        task = asyncio.create_task(session.run())
        destination = await wait_for(transport, NavigationView)
        assert isinstance(destination, NavigationView)
        session.accept_action(action(destination, "confirm_destination"))
        mode = await wait_for(transport, NavigationView, count=2)
        assert isinstance(mode, NavigationView)
        session.accept_action(action(mode, "select_mode", "walking"))
        origin = await wait_for(transport, NavigationOriginRequest)
        assert isinstance(origin, NavigationOriginRequest)
        session.accept_origin(NavigationOrigin(
            session_id=origin.session_id, request_id=origin.request_id,
            generation=origin.generation, latitude=41.9, longitude=-87.65,
        ))

        error = await wait_for(transport, NavigationView, count=5)
        assert isinstance(error, NavigationView)
        assert error.navigation.primary == "Route unavailable"
        session.cancel()
        await task

    @pytest.mark.parametrize("unordered_steps", [False, True])
    async def test_provider_route_must_be_wire_safe_and_progress_ordered(
        self, unordered_steps: bool,
    ) -> None:
        candidate = NavigationCandidate(
            choice_id="place-1", label="Place", address="1 Main St",
            point=NavigationPoint(1, 1),
        )
        transport = SinkTransport()
        session = NavigationSession(
            "Place", WireInvalidRouteProvider(
                [candidate], unordered_steps=unordered_steps,
            ), transport, session_id="nav-wire-invalid",
        )
        task = asyncio.create_task(session.run())
        destination = await wait_for(transport, NavigationView)
        assert isinstance(destination, NavigationView)
        session.accept_action(action(destination, "confirm_destination"))
        mode = await wait_for(transport, NavigationView, count=2)
        assert isinstance(mode, NavigationView)
        session.accept_action(action(mode, "select_mode", "walking"))
        origin = await wait_for(transport, NavigationOriginRequest)
        assert isinstance(origin, NavigationOriginRequest)
        session.accept_origin(NavigationOrigin(
            session_id=origin.session_id, request_id=origin.request_id,
            generation=origin.generation, latitude=41.9, longitude=-87.65,
        ))

        error = await wait_for(transport, NavigationView, count=5)
        assert isinstance(error, NavigationView)
        assert error.navigation.primary == "Route unavailable"
        session.cancel()
        await task

    async def test_rejected_navigation_view_stops_the_session_fail_closed(self) -> None:
        candidate = NavigationCandidate(
            choice_id="place-1", label="Place", address="1 Main St",
            point=NavigationPoint(1, 1),
        )
        transport = SinkTransport()
        session = NavigationSession(
            "Place", FakeProvider([candidate]), transport, session_id="nav-paint-failure",
        )
        task = asyncio.create_task(session.run())
        view = await wait_for(transport, NavigationView)
        assert isinstance(view, NavigationView)

        session.accept_view_ack(NavigationViewAck(
            session_id=view.session_id, request_id=view.request_id,
            generation=view.generation, outcome="rejected",
        ))
        await task
        assert isinstance(transport.sent[-1], NavigationStop)

    async def test_canonical_confirmation_mode_origin_preview_and_route_install(self) -> None:
        candidate = NavigationCandidate(
            choice_id="place-1", label="Cajun Corner",
            address="2301 N Lincoln Ave, Chicago",
            point=NavigationPoint(latitude=41.92, longitude=-87.65),
        )
        provider = FakeProvider([candidate])
        transport = SinkTransport()
        session = NavigationSession(
            "Cajun Corner", provider, transport, session_id="nav-test",
        )
        task = asyncio.create_task(session.run())

        destination = await wait_for(transport, NavigationView)
        assert isinstance(destination, NavigationView)
        assert destination.navigation.view == "destination"
        assert destination.navigation.primary == "Cajun Corner"
        assert "2301 N Lincoln" in destination.navigation.secondary

        session.accept_action(action(destination, "confirm_destination"))
        mode = await wait_for(transport, NavigationView, count=2)
        assert isinstance(mode, NavigationView)
        assert mode.navigation.view == "mode"
        session.accept_action(action(mode, "select_mode", "walking"))

        origin_request = await wait_for(transport, NavigationOriginRequest)
        assert isinstance(origin_request, NavigationOriginRequest)
        session.accept_origin(NavigationOrigin(
            session_id=origin_request.session_id,
            request_id=origin_request.request_id,
            generation=origin_request.generation,
            latitude=41.9, longitude=-87.65, accuracy=6,
        ))
        locating = await wait_for(transport, NavigationView, count=3)
        assert isinstance(locating, NavigationView)
        assert locating.navigation.view == "uncertain"
        assert locating.navigation.primary == "Getting your location"
        assert locating.navigation.interaction == "none"

        preview = await wait_for(transport, NavigationView, count=5)
        assert isinstance(preview, NavigationView)
        assert preview.navigation.view == "preview"
        assert preview.navigation.mode == "walking"

        session.accept_action(action(preview, "start"))
        session.accept_action(action(preview, "start"))
        starting = await wait_for(transport, NavigationView, count=6)
        assert isinstance(starting, NavigationView)
        assert starting.navigation.view == "rerouting"
        assert starting.navigation.primary == "Starting navigation"
        assert starting.navigation.interaction == "none"
        route = await wait_for(transport, NavigationRoute)
        assert isinstance(route, NavigationRoute)
        assert route.destination_label == "Cajun Corner"
        assert provider.routes[0][2] == "walking"
        session.accept_route_ack(NavigationRouteAck(
            session_id="nav-test", route_id="route-1", revision=1,
            outcome="installed",
        ))
        session.accept_state(NavigationState(
            session_id="nav-test", route_id="route-1", revision=1,
            sequence=10, state="guidance",
        ))
        session.accept_route_ack(NavigationRouteAck(
            session_id="nav-test", route_id="route-1", revision=1,
            outcome="installed",
        ))
        session.accept_state(NavigationState(
            session_id="nav-test", route_id="route-1", revision=1,
            sequence=1, state="arrived",
        ))
        await asyncio.sleep(0)
        assert session.completion_state != "arrived"
        assert len([m for m in transport.sent if isinstance(m, NavigationRoute)]) == 1
        assert session.consume_transcript("ambient room speech") is False
        assert provider.searches == ["Cajun Corner"]
        session.accept_reroute(NavigationRerouteRequest(
            session_id="nav-test", route_id="route-1", base_revision=1,
            request_id="reroute-1", latitude=41.905, longitude=-87.645,
        ))
        rerouting = await wait_for(transport, NavigationView, count=7)
        assert isinstance(rerouting, NavigationView)
        assert rerouting.navigation.view == "rerouting"
        await asyncio.sleep(0)
        assert len(provider.routes) == 1
        session.accept_view_ack(NavigationViewAck(
            session_id=rerouting.session_id,
            request_id=rerouting.request_id,
            generation=rerouting.generation,
            outcome="bridge_acknowledged",
        ))
        session.accept_state(NavigationState(
            session_id="nav-test", route_id="route-1", revision=1,
            sequence=11, state="arrived",
        ))
        session.accept_reroute(NavigationRerouteRequest(
            session_id="nav-test", route_id="route-1", base_revision=1,
            request_id="reroute-1", latitude=41.905, longitude=-87.645,
        ))
        rerouted = await wait_for(transport, NavigationRoute, count=2)
        assert isinstance(rerouted, NavigationRoute)
        assert rerouted.revision == 2
        assert session.completion_state != "arrived"
        assert len(provider.routes) == 2
        session.accept_route_ack(NavigationRouteAck(
            session_id="nav-test", route_id="route-1", revision=2,
            outcome="installed",
        ))
        session.accept_state(NavigationState(
            session_id="nav-test", route_id="route-1", revision=2,
            sequence=1, state="arrived",
        ))
        session.accept_action(NavigationAction(
            session_id="nav-test", request_id=rerouting.request_id,
            generation=rerouting.generation, action="finish",
        ))
        await task
        assert isinstance(transport.sent[-1], NavigationStop)

    async def test_ambiguous_candidate_selection_and_spoken_retry_preserve_canonical_loop(self) -> None:
        candidates = [
            NavigationCandidate(
                choice_id="west", label="Cajun Corner West",
                address="100 W Main St", point=NavigationPoint(1, 1),
                repair_hint="street direction",
            ),
            NavigationCandidate(
                choice_id="east", label="Cajun Corner East",
                address="100 E Main St", point=NavigationPoint(2, 2),
                repair_hint="street direction",
            ),
        ]
        provider = FakeProvider(candidates)
        transport = SinkTransport()
        session = NavigationSession("Cajun Corner", provider, transport, session_id="nav-test")
        task = asyncio.create_task(session.run())

        repair = await wait_for(transport, NavigationView)
        assert isinstance(repair, NavigationView)
        assert repair.navigation.view == "repair"
        assert repair.navigation.choice_ids == ["west", "east", "retry"]
        assert all(len(option) <= 64 for option in repair.navigation.options)
        repair.to_wire()
        session.accept_action(action(repair, "select_destination", "east"))
        for _ in range(100):
            views = [m for m in transport.sent if isinstance(m, NavigationView)]
            if len(views) >= 2:
                break
            await asyncio.sleep(0)
        chosen = views[-1]
        assert chosen.navigation.primary == "Cajun Corner East"

        session.accept_action(action(chosen, "retry_destination"))
        retry = await wait_for(transport, NavigationView, count=3)
        assert isinstance(retry, NavigationView)
        assert retry.navigation.primary == "Say the destination again"
        assert session.consume_transcript("100 West Main Street") is True
        for _ in range(100):
            if provider.searches[-1] == "100 West Main Street":
                break
            await asyncio.sleep(0)
        assert provider.searches == ["Cajun Corner", "100 West Main Street"]
        session.cancel()
        await task

    async def test_stale_action_cannot_advance_a_newer_destination_generation(self) -> None:
        candidate = NavigationCandidate(
            choice_id="place-1", label="Place", address="1 Main St",
            point=NavigationPoint(1, 1),
        )
        transport = SinkTransport()
        session = NavigationSession(
            "Place", FakeProvider([candidate]), transport, session_id="nav-test",
        )
        task = asyncio.create_task(session.run())
        first = await wait_for(transport, NavigationView)
        assert isinstance(first, NavigationView)
        assert session.consume_transcript("A newer place") is False
        session.accept_action(action(first, "retry_destination"))
        retry = await wait_for(transport, NavigationView, count=2)
        assert isinstance(retry, NavigationView)
        assert session.consume_transcript("A newer place") is True
        for _ in range(100):
            views = [m for m in transport.sent if isinstance(m, NavigationView)]
            if len(views) >= 3:
                break
            await asyncio.sleep(0)
        session.accept_action(action(first, "confirm_destination"))
        await asyncio.sleep(0)
        assert views[-1].navigation.view == "destination"
        assert not any(
            isinstance(message, NavigationView) and message.navigation.view == "mode"
            for message in transport.sent
        )
        session.cancel()
        await task

    async def test_cancelled_reroute_completion_cannot_install_after_session_end(self) -> None:
        candidate = NavigationCandidate(
            choice_id="place-1", label="Place", address="1 Main St",
            point=NavigationPoint(1, 1),
        )
        provider = CancellationResistantProvider([candidate])
        transport = SinkTransport()
        session = NavigationSession(
            "Place", provider, transport, session_id="nav-stale-reroute",
        )
        task = asyncio.create_task(session.run())

        destination = await wait_for(transport, NavigationView)
        assert isinstance(destination, NavigationView)
        session.accept_action(action(destination, "confirm_destination"))
        mode = await wait_for(transport, NavigationView, count=2)
        assert isinstance(mode, NavigationView)
        session.accept_action(action(mode, "select_mode", "walking"))
        origin = await wait_for(transport, NavigationOriginRequest)
        assert isinstance(origin, NavigationOriginRequest)
        session.accept_origin(NavigationOrigin(
            session_id=origin.session_id, request_id=origin.request_id,
            generation=origin.generation, latitude=41.9, longitude=-87.65,
            accuracy=5,
        ))
        preview = await wait_for(transport, NavigationView, count=5)
        assert isinstance(preview, NavigationView)
        session.accept_action(action(preview, "start"))
        installed = await wait_for(transport, NavigationRoute)
        assert isinstance(installed, NavigationRoute)
        session.accept_route_ack(NavigationRouteAck(
            session_id=installed.session_id, route_id=installed.route_id,
            revision=installed.revision, outcome="installed",
        ))
        session.accept_reroute(NavigationRerouteRequest(
            session_id=installed.session_id, route_id=installed.route_id,
            base_revision=installed.revision, request_id="reroute-stale",
            latitude=41.905, longitude=-87.645,
        ))
        rerouting = await wait_for(transport, NavigationView, count=7)
        assert isinstance(rerouting, NavigationView)
        session.accept_view_ack(NavigationViewAck(
            session_id=rerouting.session_id,
            request_id=rerouting.request_id,
            generation=rerouting.generation,
            outcome="bridge_acknowledged",
        ))
        await asyncio.wait_for(provider.reroute_started.wait(), timeout=1)

        assert session.consume_transcript("ambient new destination") is False
        session.cancel()
        provider.release_reroute.set()
        await task

        assert len([
            message for message in transport.sent
            if isinstance(message, NavigationRoute)
        ]) == 1
        assert isinstance(transport.sent[-1], NavigationStop)
