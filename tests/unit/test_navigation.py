from __future__ import annotations

import asyncio

import pytest

import joat.services.navigation as navigation_service
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
from joat.navigation.map_image import NavigationMapTile
from joat.core.protocol import NavigationMapPanel
from joat.services.navigation import NavigationSession, route_map_centres
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
        self.search_proximities: list[NavigationPoint | None] = []
        self.routes: list[tuple[NavigationPoint, NavigationCandidate, str]] = []

    async def search(
        self, query: str, proximity: NavigationPoint | None,
    ) -> list[NavigationCandidate]:
        self.searches.append(query)
        self.search_proximities.append(proximity)
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
        assert navigation_query("JOAT, navigate to the library") == "the library"
        assert navigation_query("joat directions to 2301 N Lincoln Ave") == (
            "2301 N Lincoln Ave"
        )
        assert navigation_query("Could you take me to the nearest OnCue near me?") == (
            "the nearest OnCue near me"
        )
        assert navigation_query("JOAT, please help me get to OnCue") == "OnCue"
        assert navigation_query("How do I get to the airport?") == "the airport"
        assert navigation_query("What's the best way to the library?") == "the library"
        assert navigation_query("Cajun Corner") is None
        assert navigation_query("I talked about the way to the library") is None

    async def test_nearest_request_refreshes_phone_location_and_cleans_provider_query(
        self,
    ) -> None:
        candidate = NavigationCandidate(
            choice_id="oncue-nearby",
            label="OnCue",
            address="1201 N Main St",
            point=NavigationPoint(latitude=35.480, longitude=-97.520),
        )
        provider = FakeProvider([candidate])
        refreshed = NavigationPoint(latitude=35.475, longitude=-97.520)
        refresh_calls = 0

        async def refresh_proximity() -> NavigationPoint | None:
            nonlocal refresh_calls
            refresh_calls += 1
            return refreshed

        transport = SinkTransport()
        session = NavigationSession(
            "the nearest OnCue near me",
            provider,
            transport,
            session_id="nav-nearest-oncue",
            proximity=NavigationPoint(latitude=0, longitude=0),
            refresh_proximity=refresh_proximity,
        )
        task = asyncio.create_task(session.run())

        destination = await wait_for(transport, NavigationView)
        assert isinstance(destination, NavigationView)
        assert refresh_calls == 1
        assert provider.searches == ["OnCue"]
        assert provider.search_proximities == [refreshed]
        assert "About " in destination.navigation.secondary
        assert "1201 N Main St" in destination.navigation.secondary

        assert session.consume_transcript("I meant OnCue, the gas station") is True
        for _ in range(100):
            if len(provider.searches) == 2:
                break
            await asyncio.sleep(0)
        assert refresh_calls == 1
        assert provider.searches == ["OnCue", "OnCue gas station"]
        assert provider.search_proximities == [refreshed, refreshed]

        session.cancel()
        await task

    async def test_nearest_request_does_not_claim_a_result_without_phone_location(
        self,
    ) -> None:
        provider = FakeProvider([])

        async def refresh_proximity() -> NavigationPoint | None:
            return None

        transport = SinkTransport()
        session = NavigationSession(
            "closest OnCue",
            provider,
            transport,
            session_id="nav-nearest-no-location",
            refresh_proximity=refresh_proximity,
        )
        task = asyncio.create_task(session.run())

        unavailable = await wait_for(transport, NavigationView)
        assert isinstance(unavailable, NavigationView)
        assert provider.searches == []
        assert unavailable.navigation.view == "error"
        assert unavailable.navigation.primary == "Phone location unavailable"
        assert "nearest" in unavailable.navigation.secondary.casefold()

        session.cancel()
        await task

    async def test_ordinary_destination_keeps_cached_proximity_without_refreshing(
        self,
    ) -> None:
        candidate = NavigationCandidate(
            choice_id="library",
            label="Downtown Library",
            address="300 Park Ave",
            point=NavigationPoint(latitude=35.48, longitude=-97.52),
        )
        provider = FakeProvider([candidate])
        cached = NavigationPoint(latitude=35.47, longitude=-97.51)

        async def must_not_refresh() -> NavigationPoint | None:
            raise AssertionError("ordinary destinations do not require a new phone fix")

        transport = SinkTransport()
        session = NavigationSession(
            "Downtown Library",
            provider,
            transport,
            session_id="nav-named-place",
            proximity=cached,
            refresh_proximity=must_not_refresh,
        )
        task = asyncio.create_task(session.run())

        destination = await wait_for(transport, NavigationView)
        assert isinstance(destination, NavigationView)
        assert provider.searches == ["Downtown Library"]
        assert provider.search_proximities == [cached]
        assert destination.navigation.secondary == "300 Park Ave"

        session.cancel()
        await task

    async def test_destination_view_accepts_a_narrow_conversational_correction(
        self,
    ) -> None:
        candidate = NavigationCandidate(
            choice_id="oncue",
            label="OnCue",
            address="1201 N Main St",
            point=NavigationPoint(latitude=35.48, longitude=-97.52),
        )
        provider = FakeProvider([candidate])
        transport = SinkTransport()
        session = NavigationSession(
            "Onsen",
            provider,
            transport,
            session_id="nav-spoken-correction",
        )
        task = asyncio.create_task(session.run())
        await wait_for(transport, NavigationView)

        assert session.consume_transcript("No, I mean OnCue, the gas station") is True
        for _ in range(100):
            if len(provider.searches) == 2:
                break
            await asyncio.sleep(0)

        assert provider.searches == ["Onsen", "OnCue gas station"]
        assert session.consume_transcript("That gas station is new") is False

        session.cancel()
        await task

    async def test_voice_controls_destination_mode_and_retry_from_the_visible_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            navigation_service,
            "_MIN_LOCATING_VISIBLE_SECONDS",
            0,
            raising=False,
        )
        candidate = NavigationCandidate(
            choice_id="place-1", label="Cajun Corner",
            address="2301 N Lincoln Ave, Chicago",
            point=NavigationPoint(latitude=41.92, longitude=-87.65),
        )
        transport = SinkTransport()
        session = NavigationSession(
            "Cajun Corner", FakeProvider([candidate]), transport,
            session_id="nav-voice-controls",
        )
        task = asyncio.create_task(session.run())

        destination = await wait_for(transport, NavigationView)
        assert isinstance(destination, NavigationView)
        assert destination.navigation.view == "destination"
        assert session.consume_transcript("confirm") is True

        mode = await wait_for(transport, NavigationView, count=2)
        assert isinstance(mode, NavigationView)
        assert mode.navigation.view == "mode"
        assert session.consume_transcript("cycle") is True

        origin = await wait_for(transport, NavigationOriginRequest)
        assert isinstance(origin, NavigationOriginRequest)
        session.accept_origin_failure(NavigationOriginFailure(
            session_id=origin.session_id, request_id=origin.request_id,
            generation=origin.generation, reason="unavailable",
        ))
        error = await wait_for(transport, NavigationView, count=4)
        assert isinstance(error, NavigationView)
        assert error.navigation.primary == "Phone location unavailable"
        assert session.consume_transcript("try again") is True

        retried = await wait_for(transport, NavigationOriginRequest, count=2)
        assert isinstance(retried, NavigationOriginRequest)
        session.cancel()
        await task

    async def test_voice_can_choose_a_candidate_replace_destination_start_and_finish(
        self,
    ) -> None:
        candidates = [
            NavigationCandidate(
                choice_id="west", label="Cajun Corner West",
                address="100 W Main St", point=NavigationPoint(1, 1),
            ),
            NavigationCandidate(
                choice_id="east", label="Cajun Corner East",
                address="100 E Main St", point=NavigationPoint(2, 2),
            ),
        ]
        provider = FakeProvider(candidates)
        transport = SinkTransport()
        session = NavigationSession(
            "Cajun Corner", provider, transport, session_id="nav-voice-complete",
        )
        task = asyncio.create_task(session.run())

        repair = await wait_for(transport, NavigationView)
        assert isinstance(repair, NavigationView)
        assert repair.navigation.view == "repair"
        assert session.consume_transcript("second") is True
        destination = await wait_for(transport, NavigationView, count=2)
        assert isinstance(destination, NavigationView)
        assert destination.navigation.primary == "Cajun Corner East"
        assert session.consume_transcript("confirm") is True
        mode = await wait_for(transport, NavigationView, count=3)
        assert isinstance(mode, NavigationView)
        assert session.consume_transcript("walk") is True
        origin = await wait_for(transport, NavigationOriginRequest)
        assert isinstance(origin, NavigationOriginRequest)
        session.accept_origin(NavigationOrigin(
            session_id=origin.session_id, request_id=origin.request_id,
            generation=origin.generation, latitude=41.9, longitude=-87.65,
        ))
        preview = await wait_for(transport, NavigationView, count=6)
        assert isinstance(preview, NavigationView)
        assert preview.navigation.view == "preview"
        assert session.consume_transcript("start navigation") is True
        route = await wait_for(transport, NavigationRoute)
        assert isinstance(route, NavigationRoute)
        session.accept_route_ack(NavigationRouteAck(
            session_id=route.session_id, route_id=route.route_id,
            revision=route.revision, outcome="installed",
        ))
        session.accept_state(NavigationState(
            session_id=route.session_id, route_id=route.route_id,
            revision=route.revision, sequence=1, state="arrived",
        ))
        await asyncio.sleep(0)
        assert session.consume_transcript("done") is True
        await task
        assert isinstance(transport.sent[-1], NavigationStop)

    async def test_voice_can_replace_an_active_destination_or_stop_navigation(self) -> None:
        candidate = NavigationCandidate(
            choice_id="place-1", label="Cajun Corner", address="2301 N Lincoln Ave",
            point=NavigationPoint(1, 1),
        )
        provider = FakeProvider([candidate])
        transport = SinkTransport()
        session = NavigationSession(
            "Cajun Corner", provider, transport, session_id="nav-voice-replace",
        )
        task = asyncio.create_task(session.run())
        await wait_for(transport, NavigationView)

        assert session.consume_transcript("JOAT, navigate to the library") is True
        for _ in range(100):
            if provider.searches[-1:] == ["the library"]:
                break
            await asyncio.sleep(0)
        assert provider.searches == ["Cajun Corner", "the library"]
        assert session.consume_transcript("stop navigation") is True
        await task
        assert isinstance(transport.sent[-1], NavigationStop)

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

    async def test_missing_provider_closes_on_voice_cancel(self) -> None:
        transport = SinkTransport()
        session = NavigationUnavailableSession(transport, session_id="nav-offline-voice")
        task = asyncio.create_task(session.run())
        await wait_for(transport, NavigationView)

        assert session.consume_transcript("cancel navigation") is True

        await task
        assert isinstance(transport.sent[-1], NavigationStop)

    async def test_missing_provider_does_not_consume_an_unrelated_voice_request(self) -> None:
        transport = SinkTransport()
        session = NavigationUnavailableSession(transport, session_id="nav-offline-unrelated")
        task = asyncio.create_task(session.run())
        await wait_for(transport, NavigationView)

        assert session.consume_transcript("what is on my calendar") is False
        assert not task.done()

        session.cancel()
        await task

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

    async def test_origin_failure_keeps_the_locating_state_visible_before_recovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            navigation_service,
            "_MIN_LOCATING_VISIBLE_SECONDS",
            0.05,
            raising=False,
        )
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
        await asyncio.sleep(0)
        locating_views = [
            message for message in transport.sent
            if isinstance(message, NavigationView)
        ]
        assert len(locating_views) == 3
        assert locating_views[-1].navigation.primary == "Getting your location"

        await asyncio.sleep(0.06)
        error = await wait_for(transport, NavigationView, count=4)
        assert isinstance(error, NavigationView)
        assert error.navigation.primary == "Phone location unavailable"
        assert error.navigation.secondary == "Allow Location for Even Hub, then retry"
        assert error.navigation.hint == "tap/say retry · swipe cancel"
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
        assert destination.navigation.hint == "tap/say confirm · swipe retry"

        session.accept_action(action(destination, "confirm_destination"))
        mode = await wait_for(transport, NavigationView, count=2)
        assert isinstance(mode, NavigationView)
        assert mode.navigation.view == "mode"
        assert mode.navigation.hint == "tap choose · swipe select · say walk/cycle"
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
        assert preview.navigation.hint == "tap/say start · swipe up/down to edit"

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
        assert repair.navigation.hint == "tap choose · swipe select · say first/second"
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


class _FailingProvider(FakeProvider):
    """A provider whose route failures carry the documented reason code."""

    def __init__(self, candidates: list[NavigationCandidate], reason: str) -> None:
        super().__init__(candidates)
        self._reason = reason

    async def route(
        self, origin: NavigationPoint, destination: NavigationCandidate,
        mode: NavigationMode,
    ) -> ProviderRoute:
        error = RuntimeError("bounded provider failure")
        setattr(error, "reason", self._reason)
        raise error


class TestRouteFailuresNameTheirCause:
    """One blind "Route unavailable" taught the wearer nothing. Each cause
    must name a different next move on the lens."""

    @pytest.mark.parametrize(("reason", "primary", "secondary"), [
        ("no_route", "No walking route from here", "Try cycling or a closer destination"),
        ("too_long", "Route too long for the HUD", "Choose a closer destination"),
        ("unreachable", "Can't reach the map service", "Check this Mac's internet, then retry"),
        ("incomplete", "Route data was incomplete", "Try again or change destination"),
    ])
    async def test_each_failure_reason_gets_actionable_copy(
        self, reason: str, primary: str, secondary: str,
    ) -> None:
        candidate = NavigationCandidate(
            choice_id="place", label="Place", address="1 Main",
            point=NavigationPoint(41.95, -87.60),
        )
        transport = SinkTransport()
        session = NavigationSession(
            "Place", _FailingProvider([candidate], reason), transport,
            session_id=f"nav-{reason}",
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
        assert error.navigation.view == "error"
        assert error.navigation.primary == primary
        assert error.navigation.secondary == secondary
        # Every one stays recoverable without leaving the session.
        assert error.navigation.hint == "tap/say retry · swipe cancel"
        session.cancel()
        await task


def _metres(a: NavigationPoint, b: NavigationPoint) -> float:
    return navigation_service.map_move_meters(a.latitude, a.longitude, b.latitude, b.longitude)


class TestRouteMapCentres:
    """The street tiles are fetched ALONG the route, every stride of road.

    The Mac receives no live position during guidance, so it cannot follow
    the wearer with tile fetches; it covers the route up front instead and
    the plugin draws the tile nearest the wearer.
    """

    def test_a_straight_kilometre_gets_a_tile_every_stride_and_one_at_the_end(self) -> None:
        start = NavigationCoordinate(latitude=0.0, longitude=0.0)
        end = NavigationCoordinate(latitude=0.0, longitude=0.009)  # ~1002 m
        centres = route_map_centres([start, end], stride_m=180)
        assert len(centres) == 7
        assert centres[0] == NavigationPoint(0.0, 0.0)
        gaps = [_metres(a, b) for a, b in zip(centres, centres[1:])]
        assert all(abs(gap - 180) < 1 for gap in gaps[:-1])
        assert _metres(centres[-1], NavigationPoint(0.0, 0.009)) < 1

    def test_a_long_route_gets_a_longer_stride_instead_of_more_tiles(self) -> None:
        start = NavigationCoordinate(latitude=0.0, longitude=0.0)
        end = NavigationCoordinate(latitude=0.0, longitude=0.09)  # ~10 km
        centres = route_map_centres([start, end], stride_m=180, max_tiles=24)
        assert len(centres) == 24
        gaps = [_metres(a, b) for a, b in zip(centres, centres[1:])]
        assert min(gaps) > 400

    def test_a_short_hop_is_one_tile_and_a_bend_is_walked_not_sampled(self) -> None:
        start = NavigationCoordinate(latitude=0.0, longitude=0.0)
        near = NavigationCoordinate(latitude=0.0, longitude=0.0001)  # ~11 m
        assert route_map_centres([start, near]) == [NavigationPoint(0.0, 0.0)]
        bend = [
            NavigationCoordinate(latitude=0.0, longitude=0.0),
            NavigationCoordinate(latitude=0.0, longitude=0.0027),   # ~300 m east
            NavigationCoordinate(latitude=0.0027, longitude=0.0027),  # ~300 m north
        ]
        centres = route_map_centres(bend, stride_m=180)
        # 0, 180 (on the first leg), 360 (60 m up the second leg), 540, end.
        assert len(centres) == 5
        assert centres[2].longitude == pytest.approx(0.0027)
        assert 0.0 < centres[2].latitude < 0.0027


class TestRouteTilePrefetch:
    @pytest.mark.asyncio
    async def test_the_streets_along_the_route_are_fetched_once_the_route_is_known(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fetched: list[tuple[float, float]] = []

        async def fake_tile(token: str, latitude: float, longitude: float, **_: object) -> NavigationMapTile:
            assert token == "map-token"
            fetched.append((latitude, longitude))
            return NavigationMapTile(
                latitude=latitude, longitude=longitude, zoom=16, width=284, height=220,
                png=b"\x89PNG fake",
            )

        monkeypatch.setattr(navigation_service, "render_map_tile", fake_tile)
        candidate = NavigationCandidate(
            choice_id="place-1", label="Cajun Corner",
            address="2301 N Lincoln Ave, Chicago",
            point=NavigationPoint(latitude=41.92, longitude=-87.65),
        )
        provider = FakeProvider([candidate])
        transport = SinkTransport()
        session = NavigationSession(
            "Cajun Corner", provider, transport, session_id="nav-test", map_token="map-token",
        )
        task = asyncio.create_task(session.run())
        try:
            destination = await wait_for(transport, NavigationView)
            assert isinstance(destination, NavigationView)
            session.accept_action(action(destination, "confirm_destination"))
            mode = await wait_for(transport, NavigationView, count=2)
            assert isinstance(mode, NavigationView)
            session.accept_action(action(mode, "select_mode", "walking"))
            origin_request = await wait_for(transport, NavigationOriginRequest)
            assert isinstance(origin_request, NavigationOriginRequest)
            session.accept_origin(NavigationOrigin(
                session_id=origin_request.session_id,
                request_id=origin_request.request_id,
                generation=origin_request.generation,
                latitude=41.9, longitude=-87.65, accuracy=6,
            ))
            preview = await wait_for(transport, NavigationView, count=5)
            assert isinstance(preview, NavigationView)
            assert preview.navigation.view == "preview"
            # Let the one-at-a-time fetch worker drain the route's centres.
            for _ in range(40):
                await asyncio.sleep(0)
            panels = [m for m in transport.sent if isinstance(m, NavigationMapPanel)]
            # The plan runs ~836 m: the origin tile, then every 180 m, then
            # the end — and the origin is not fetched twice when the route's
            # first centre lands on it.
            centres = route_map_centres(plan().geometry)
            assert len(panels) == len(centres) == 6
            assert panels[0].latitude == 41.9 and panels[0].longitude == -87.65
            assert fetched == [(p.latitude, p.longitude) for p in panels]
            assert all(p.width == 284 and p.height == 220 and p.zoom == 16 for p in panels)
            assert len({(round(p.latitude, 6), round(p.longitude, 6)) for p in panels}) == 6
            assert all(p.session_id == "nav-test" for p in panels)
        finally:
            session.cancel()
            await task


class TestSavedPlaces:
    @pytest.mark.asyncio
    async def test_a_pinned_saved_place_skips_the_provider_and_keeps_its_name(self) -> None:
        from joat.core.protocol import SavedPlace

        saved = [SavedPlace(place_id="p1", name="Home", address="12 Oak St", latitude=41.92, longitude=-87.65)]
        provider = FakeProvider([])
        transport = SinkTransport()
        session = NavigationSession(
            "my home", provider, transport, session_id="nav-test", places=lambda: saved,
        )
        task = asyncio.create_task(session.run())
        try:
            destination = await wait_for(transport, NavigationView)
            assert isinstance(destination, NavigationView)
            assert destination.navigation.view == "destination"
            assert destination.navigation.primary == "Home"
            assert destination.navigation.secondary == "12 Oak St"
            assert provider.searches == []
        finally:
            session.cancel()
            await task

    @pytest.mark.asyncio
    async def test_an_address_only_saved_place_searches_its_address_under_its_name(self) -> None:
        from joat.core.protocol import SavedPlace

        saved = [SavedPlace(place_id="p2", name="Work", address="2301 N Lincoln Ave, Chicago")]
        candidate = NavigationCandidate(
            choice_id="place-1", label="Cajun Corner",
            address="2301 N Lincoln Ave, Chicago",
            point=NavigationPoint(latitude=41.92, longitude=-87.65),
        )
        provider = FakeProvider([candidate])
        transport = SinkTransport()
        session = NavigationSession(
            "Work", provider, transport, session_id="nav-test", places=lambda: saved,
        )
        task = asyncio.create_task(session.run())
        try:
            destination = await wait_for(transport, NavigationView)
            assert isinstance(destination, NavigationView)
            assert destination.navigation.view == "destination"
            assert destination.navigation.primary == "Work"
            assert "2301 N Lincoln" in destination.navigation.secondary
            assert provider.searches == ["2301 N Lincoln Ave, Chicago"]
        finally:
            session.cancel()
            await task

    @pytest.mark.asyncio
    async def test_an_unknown_name_still_goes_to_the_provider(self) -> None:
        from joat.core.protocol import SavedPlace

        saved = [SavedPlace(place_id="p1", name="Home", address="12 Oak St")]
        candidate = NavigationCandidate(
            choice_id="place-1", label="Cajun Corner",
            address="2301 N Lincoln Ave, Chicago",
            point=NavigationPoint(latitude=41.92, longitude=-87.65),
        )
        provider = FakeProvider([candidate])
        transport = SinkTransport()
        session = NavigationSession(
            "Cajun Corner", provider, transport, session_id="nav-test", places=lambda: saved,
        )
        task = asyncio.create_task(session.run())
        try:
            destination = await wait_for(transport, NavigationView)
            assert isinstance(destination, NavigationView)
            assert destination.navigation.primary == "Cajun Corner"
            assert provider.searches == ["Cajun Corner"]
        finally:
            session.cancel()
            await task
