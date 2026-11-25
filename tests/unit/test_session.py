"""SessionManager unit tests: active session arbitration, notifications, verbs, and terminal actions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from typing import Any

import pytest

from joat.channels.cards import Card, CardLog, Urgency
from joat.core.interfaces import GlassesTransport
from joat.core.protocol import (
    AssistantOverlay,
    DownlinkMessage,
    HudText,
    TerminalAction,
)
from joat.services.hud import HudWriter
from joat.services.session import (
    MAX_NAVIGATION_DESTINATION_CHARS,
    Session,
    SessionManager,
    SessionStartResult,
    notification_stack,
)


class SinkTransport(GlassesTransport):
    def __init__(self) -> None:
        self.sent: list[DownlinkMessage] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)

    @property
    def connected(self) -> bool:
        return True

    def on_audio(self, callback: object) -> None: ...  # type: ignore[override]
    def on_input(self, callback: object) -> None: ...  # type: ignore[override]
    def on_device(self, callback: object) -> None: ...  # type: ignore[override]


async def _noop() -> str:
    return ""


class TestSessionManager:
    async def test_session_revision_survives_completion_and_counts_admitted_starts_only(self) -> None:
        class ImmediateSession(Session):
            async def run(self) -> None:
                return None

            def cancel(self) -> None:
                return None

        observed: list[tuple[str, int]] = []
        manager = SessionManager(
            HudWriter(SinkTransport()),
            on_state=lambda state: observed.append((state, manager.session_revision)),
            navigation_factory=lambda _dest: ImmediateSession(),
        )
        try:
            assert manager.session_revision == 0
            assert manager.start_navigation("") is SessionStartResult.INVALID
            assert manager.session_revision == 0
            for revision in (1, 2):
                assert manager.try_start_session(ImmediateSession(), publish_state=True)
                assert manager.session_revision == revision
                assert not manager.try_start_session(ImmediateSession(), publish_state=True)
                assert manager.session_revision == revision
                await asyncio.sleep(0)
                assert not manager.active
                assert manager.session_revision == revision
            assert observed == [
                ("running", 1), ("settled", 1), ("running", 2), ("settled", 2),
            ]
        finally:
            await manager.close()

    async def test_start_navigation_validates_destination_and_respects_single_owner(self) -> None:
        destinations: list[str] = []
        release = asyncio.Event()

        class HeldNavSession(Session):
            async def run(self) -> None:
                await release.wait()

            def cancel(self) -> None:
                release.set()

        session = HeldNavSession()
        manager = SessionManager(
            HudWriter(SinkTransport()),
            navigation_factory=lambda dest: destinations.append(dest) or session,
        )
        try:
            assert manager.start_navigation("  Café 2301  ") is SessionStartResult.STARTED
            assert manager.active_session is session
            assert destinations == ["Café 2301"]
            assert manager.start_navigation("Replacement") is SessionStartResult.BUSY
            assert destinations == ["Café 2301"]
            assert manager.active_session is session
        finally:
            release.set()
            await manager.close()

    @pytest.mark.parametrize(
        "dest",
        ["", " \n", "bad\x00dest", "bad\ud800dest", "a" * (MAX_NAVIGATION_DESTINATION_CHARS + 1)],
    )
    async def test_start_navigation_invalid_destination_never_calls_factory(self, dest: str) -> None:
        destinations: list[str] = []
        manager = SessionManager(
            HudWriter(SinkTransport()),
            navigation_factory=lambda d: destinations.append(d) or None,
        )
        try:
            assert manager.start_navigation(dest) is SessionStartResult.INVALID
            assert destinations == []
            assert not manager.active
        finally:
            await manager.close()

    async def test_start_navigation_unavailable_factory_and_shutdown_are_not_starts(self) -> None:
        ready = False
        release = asyncio.Event()

        class HeldNavSession(Session):
            async def run(self) -> None:
                await release.wait()

            def cancel(self) -> None:
                release.set()

        no_factory = SessionManager(HudWriter(SinkTransport()))
        assert no_factory.start_navigation("Airport") is SessionStartResult.UNAVAILABLE
        await no_factory.close()

        def failing_factory(_dest: str) -> Session | None:
            raise OSError("maps failure")

        failing = SessionManager(HudWriter(SinkTransport()), navigation_factory=failing_factory)
        assert failing.start_navigation("Airport") is SessionStartResult.UNAVAILABLE
        await failing.close()

        manager = SessionManager(
            HudWriter(SinkTransport()),
            navigation_factory=lambda _dest: HeldNavSession() if ready else None,
        )
        try:
            assert manager.start_navigation("Airport") is SessionStartResult.UNAVAILABLE
            assert not manager.active
            ready = True
            assert manager.start_navigation("Airport") is SessionStartResult.STARTED
        finally:
            release.set()
            await manager.close()
        assert manager.start_navigation("After close") is SessionStartResult.CLOSED

    async def test_typed_external_session_cannot_replace_the_active_wearer_session(self) -> None:
        entered = asyncio.Event()

        class HeldSession(Session):
            async def run(self) -> None:
                entered.set()
                await asyncio.Event().wait()

            def cancel(self) -> None:
                return None

        manager = SessionManager(HudWriter(SinkTransport()))
        first = HeldSession()
        assert manager.try_start_session(first)
        await entered.wait()
        assert not manager.try_start_session(HeldSession())
        assert manager.active_session is first
        await manager.close(timeout_s=0.1)

    async def test_glasses_retry_action_uses_only_the_registered_terminal_recovery(self) -> None:
        retried = asyncio.Event()

        async def retry() -> None:
            retried.set()

        manager = SessionManager(
            HudWriter(SinkTransport()),
            terminal_retry=retry,
        )
        manager.handle_terminal_action(TerminalAction("retry_terminal"))
        await asyncio.wait_for(retried.wait(), 1.0)
        await manager.close()

    async def test_registered_session_starter_and_active_correction_share_one_owner(self) -> None:
        received: list[str] = []
        states: list[str] = []
        running = asyncio.Event()

        class NavigationSession(Session):
            async def run(self) -> None:
                running.set()
                await asyncio.Event().wait()

            def cancel(self) -> None:
                return None

            def consume_transcript(self, text: str) -> bool:
                received.append(text)
                return True

        manager = SessionManager(
            HudWriter(SinkTransport()),
            starters=[
                lambda text: NavigationSession()
                if text.lower().startswith("navigate to ") else None,
            ],
            on_state=states.append,
        )
        assert manager.handle_transcript("navigate to Corner Bakery") is True
        await running.wait()
        assert manager.handle_transcript("the street number is 2301") is True
        assert received == ["the street number is 2301"]
        assert states == []
        await manager.close(timeout_s=0.01)

    async def test_publishes_running_and_settled_lifecycle(self) -> None:
        states: list[str] = []

        class FastSession(Session):
            async def run(self) -> None: ...

            def cancel(self) -> None: ...

        manager = SessionManager(
            HudWriter(SinkTransport()),
            on_state=states.append,
        )

        assert manager.try_start_session(FastSession(), publish_state=True)
        await asyncio.sleep(0)
        assert states == ["running", "settled"]
        await manager.close()

    async def test_crashed_session_publishes_failed_not_settled(self) -> None:
        states: list[str] = []

        class CrashedSession(Session):
            async def run(self) -> None:
                raise RuntimeError("boom")

            def cancel(self) -> None: ...

        manager = SessionManager(
            HudWriter(SinkTransport()),
            on_state=states.append,
        )

        assert manager.try_start_session(CrashedSession(), publish_state=True)
        await asyncio.sleep(0)
        assert states == ["running", "failed"]
        await manager.close()

    async def test_close_cancels_and_awaits_active_session(self) -> None:
        states: list[str] = []

        class ActiveSession(Session):
            def __init__(self) -> None:
                self.cancelled = asyncio.Event()

            async def run(self) -> None:
                await self.cancelled.wait()

            def cancel(self) -> None:
                self.cancelled.set()

            @property
            def completion_state(self) -> str:
                return "cancelled"

        manager = SessionManager(
            HudWriter(SinkTransport()),
            on_state=states.append,
        )

        assert manager.try_start_session(ActiveSession(), publish_state=True)
        await asyncio.sleep(0)
        await manager.close()

        assert not manager.active
        assert states == ["running", "cancelled"]

    async def test_spoken_stop_interrupts_a_session_blocked_in_remote_io(self) -> None:
        entered = asyncio.Event()
        exited = asyncio.Event()

        class BlockedSession(Session):
            async def run(self) -> None:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    exited.set()

            def cancel(self) -> None:
                return None

        manager = SessionManager(HudWriter(SinkTransport()))

        assert manager.try_start_session(BlockedSession())
        await entered.wait()
        assert manager.handle_transcript("stop")
        async with asyncio.timeout(0.1):
            await exited.wait()
            while manager.active:
                await asyncio.sleep(0)

        assert not manager.active
        await manager.close()

    async def test_incoming_card_is_typed_as_notification_not_assistant_reply(self) -> None:
        transport = SinkTransport()
        manager = SessionManager(HudWriter(transport))

        manager.notify(Card(source="Evan", channel="Messages", summary="On my way"))
        await asyncio.sleep(0)

        cards = [m for m in transport.sent if isinstance(m, HudText)]
        assert len(cards) == 1
        assert cards[0].content_kind == "notification"
        await manager.close()

    async def test_brightness_comes_from_urgency_not_a_rendered_prefix(self) -> None:
        transport = SinkTransport()
        manager = SessionManager(HudWriter(transport))

        manager.notify(Card(source="Evan", channel="Messages", summary="Call me",
                            urgency=Urgency.URGENT))
        await asyncio.sleep(0)
        manager.notify(Card(source="News", channel="News", summary="Digest ready",
                            urgency=Urgency.NOTIFY))
        await asyncio.sleep(0)

        cards = [m for m in transport.sent if isinstance(m, HudText)]
        assert [c.text_color for c in cards] == [4, 3]
        await manager.close()

    async def test_notify_and_wait_completes_only_after_hud_write(self) -> None:
        gate = asyncio.Event()

        class GatedTransport(SinkTransport):
            async def send(self, message: DownlinkMessage) -> None:
                await gate.wait()
                await super().send(message)

        transport = GatedTransport()
        manager = SessionManager(HudWriter(transport))
        delivery = asyncio.create_task(manager.notify_and_wait(
            Card(source="Ada", channel="Messages", summary="Hi"),
        ))
        await asyncio.sleep(0)

        assert not delivery.done()
        assert transport.sent == []
        gate.set()
        await delivery
        assert len(transport.sent) == 1
        await manager.close()

    async def test_history_records_each_card_including_a_drained_batch(self) -> None:
        transport = SinkTransport()
        card_log = CardLog()

        class Held(Session):
            def __init__(self) -> None:
                self.gate = asyncio.Event()

            async def run(self) -> None:
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        held = Held()
        manager = SessionManager(HudWriter(transport), card_log=card_log)

        manager.notify(Card(source="Ada", channel="Messages", summary="first"))
        await asyncio.sleep(0)
        assert [c.source for c in card_log.recent(5)] == ["Ada"]

        assert manager.try_start_session(held) is True
        await asyncio.sleep(0)
        manager.notify(Card(source="Evan", channel="Messages", summary="queued one"))
        manager.notify(Card(source="Morgan", channel="Messages", summary="queued two"))
        held.gate.set()
        for _ in range(6):
            await asyncio.sleep(0)

        assert [c.source for c in card_log.recent(5)] == ["Ada", "Evan", "Morgan"]
        await manager.close()

    async def test_an_abandoned_queued_card_is_withdrawn_not_painted_later(self) -> None:
        transport = SinkTransport()

        class Held(Session):
            def __init__(self) -> None:
                self.gate = asyncio.Event()

            async def run(self) -> None:
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        held = Held()
        manager = SessionManager(HudWriter(transport))
        assert manager.try_start_session(held) is True
        await asyncio.sleep(0)

        card = Card(source="Design review", channel="Calendar", summary="Starts 16:30")
        delivery = asyncio.create_task(manager.notify_and_wait(card))
        await asyncio.sleep(0)
        assert not delivery.done()

        delivery.cancel()
        try:
            await delivery
        except asyncio.CancelledError:
            pass

        held.gate.set()
        for _ in range(5):
            await asyncio.sleep(0)

        painted = [m for m in transport.sent if isinstance(m, HudText)]
        assert not any("Design review" in m.text for m in painted)
        await manager.close()

    async def test_starter_starts_and_consumes(self) -> None:
        transport = SinkTransport()
        started: list[str] = []

        class FastSession(Session):
            def __init__(self, dest: str) -> None:
                self.dest = dest
                self.gate = asyncio.Event()

            async def run(self) -> None:
                started.append(self.dest)
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        mgr = SessionManager(
            HudWriter(transport),
            starters=[
                lambda text: FastSession(text[len("navigate to "):].strip("."))
                if text.lower().startswith("navigate to ") else None,
            ],
        )
        assert mgr.handle_transcript("hello there") is False
        assert mgr.handle_transcript("Navigate to Austin Airport.") is True
        await asyncio.sleep(0)
        assert started == ["Austin Airport"]
        assert mgr.active
        assert mgr.handle_transcript("what is the weather") is True
        assert mgr.handle_transcript("stop") is True
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not mgr.active
        assert mgr.handle_transcript("hello again") is False
        await mgr.close()

    async def test_a_wedged_session_releases_the_wearer_instead_of_trapping_them(self) -> None:
        from joat.services import session as session_module

        transport = SinkTransport()

        class Wedged(Session):
            def __init__(self) -> None:
                self.release = asyncio.Event()

            async def run(self) -> None:
                while not self.release.is_set():
                    try:
                        await asyncio.wait_for(self.release.wait(), 0.005)
                    except (TimeoutError, asyncio.CancelledError):
                        continue

            def cancel(self) -> None:
                return None

        wedged = Wedged()
        mgr = SessionManager(HudWriter(transport))
        assert mgr.try_start_session(wedged) is True
        await asyncio.sleep(0)
        assert mgr.active

        original = session_module.CANCEL_GRACE_S
        session_module.CANCEL_GRACE_S = 0.01
        try:
            assert mgr.handle_transcript("stop") is True
            async with asyncio.timeout(1.0):
                while mgr.active:
                    await asyncio.sleep(0.01)
        finally:
            session_module.CANCEL_GRACE_S = original

        assert mgr.handle_transcript("open planning") is False
        wedged.release.set()
        await asyncio.sleep(0.02)
        await mgr.close()

    async def test_a_long_press_cancel_reaches_the_terminal_and_says_its_receipt(self) -> None:
        transport = SinkTransport()
        cancels: list[str] = []

        async def cancel() -> str:
            cancels.append("cancel")
            return "Cancelled work."

        mgr = SessionManager(HudWriter(transport), terminal_cancel=cancel)
        said: list[str] = []

        async def say(text: str) -> None:
            said.append(text)

        mgr._say = say  # type: ignore[method-assign]
        mgr.handle_terminal_action(TerminalAction("cancel_terminal"))
        for _ in range(10):
            await asyncio.sleep(0)
        assert cancels == ["cancel"] and said == ["Cancelled work."]
        await mgr.close()

    async def test_queued_cards_stay_bounded_during_a_long_session(self) -> None:
        transport = SinkTransport()

        class Held(Session):
            def __init__(self) -> None:
                self.gate = asyncio.Event()

            async def run(self) -> None:
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        held = Held()
        mgr = SessionManager(HudWriter(transport))
        assert mgr.try_start_session(held) is True
        await asyncio.sleep(0)

        waiters = [
            asyncio.create_task(mgr.notify_and_wait(
                Card(source=f"s{n}", channel="Messages", summary=f"m{n}"),
            ))
            for n in range(20)
        ]
        for _ in range(4):
            await asyncio.sleep(0)

        assert len(mgr._pending_cards) == 6
        displaced = [t for t in waiters if t.done()]
        assert len(displaced) == 14
        assert all(isinstance(t.exception(), RuntimeError) for t in displaced)

        held.gate.set()
        outcomes = await asyncio.gather(*waiters, return_exceptions=True)
        assert sum(1 for o in outcomes if o is None) == 6
        await mgr.close()

    async def test_natural_cancel_phrases_end_a_live_session(self) -> None:
        for phrase in ("JOAT, stop", "joat stop it", "Cancel that.", "Stop it!"):
            transport = SinkTransport()

            class Held(Session):
                def __init__(self) -> None:
                    self.gate = asyncio.Event()

                async def run(self) -> None:
                    await self.gate.wait()

                def cancel(self) -> None:
                    self.gate.set()

            mgr = SessionManager(HudWriter(transport))
            assert mgr.try_start_session(Held()) is True
            await asyncio.sleep(0)
            assert mgr.active
            assert mgr.handle_transcript(phrase) is True
            for _ in range(4):
                await asyncio.sleep(0)
            assert not mgr.active, f"{phrase!r} did not cancel"
            await mgr.close()

    async def test_a_sentence_containing_stop_does_not_cancel(self) -> None:
        transport = SinkTransport()

        class Held(Session):
            def __init__(self) -> None:
                self.gate = asyncio.Event()

            async def run(self) -> None:
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        held = Held()
        mgr = SessionManager(HudWriter(transport))
        assert mgr.try_start_session(held) is True
        await asyncio.sleep(0)
        assert mgr.handle_transcript("we should stop shipping on Fridays") is True
        await asyncio.sleep(0)
        assert mgr.active
        held.gate.set()
        await mgr.close()

    async def test_direction_reaches_the_session_without_cancelling_it(self) -> None:
        directed: list[str] = []

        class Chairable(Session):
            async def run(self) -> None:
                await asyncio.Event().wait()

            def cancel(self) -> None: ...

            def direct(self, text: str) -> str | None:
                if "turn left" in text.lower():
                    directed.append(text)
                    return "navigation"
                return None

        mgr = SessionManager(HudWriter(SinkTransport()))
        assert mgr.try_start_session(Chairable()) is True
        await asyncio.sleep(0)
        assert mgr.handle_transcript("turn left here") is True
        assert directed == ["turn left here"]
        assert mgr.active
        await mgr.close(timeout_s=0.05)


class TestActiveSessionAddressGate:
    class Steerable(Session):
        def __init__(self) -> None:
            self.directed: list[str] = []
            self.consumed: list[str] = []
            self.cancelled = False

        async def run(self) -> None:
            await asyncio.Event().wait()

        def cancel(self) -> None:
            self.cancelled = True

        def consume_transcript(self, text: str) -> bool:
            self.consumed.append(text)
            return False

        def direct(self, text: str) -> str | None:
            if "claude" in text.lower():
                self.directed.append(text)
                return "claude"
            return None

    async def _active(self) -> tuple[SessionManager, Steerable]:
        session = self.Steerable()
        mgr = SessionManager(HudWriter(SinkTransport()))
        assert mgr.try_start_session(session) is True
        await asyncio.sleep(0)
        return mgr, session

    async def test_unaddressed_steering_is_ignored(self) -> None:
        mgr, session = await self._active()
        assert mgr.handle_transcript("ask claude about that", addressed=None) is True
        assert session.directed == []
        assert session.consumed == []
        assert mgr.active
        await mgr.close(timeout_s=0.05)

    async def test_addressed_steering_works(self) -> None:
        mgr, session = await self._active()
        assert mgr.handle_transcript(
            "JOAT, ask claude about that", addressed="ask claude about that",
        ) is True
        assert session.directed == ["ask claude about that"]
        await mgr.close(timeout_s=0.05)

    async def test_cancel_is_heard_unaddressed_on_purpose(self) -> None:
        mgr, session = await self._active()
        assert mgr.handle_transcript("stop", addressed=None) is True
        assert session.cancelled is True
        await mgr.close(timeout_s=0.05)

    async def test_callers_that_omit_address_keep_the_old_behaviour(self) -> None:
        mgr, session = await self._active()
        assert mgr.handle_transcript("ask claude about that") is True
        assert session.directed == ["ask claude about that"]
        await mgr.close(timeout_s=0.05)


class TestTheVerbLayer:
    class Speaking(Session):
        def __init__(self) -> None:
            self.consumed: list[str] = []

        async def run(self) -> None:
            await asyncio.Event().wait()

        def cancel(self) -> None:
            pass

        def consume_transcript(self, text: str) -> bool:
            self.consumed.append(text)
            return True

        def direct(self, text: str) -> str | None:
            return None

        def last_reply(self) -> tuple[str, str]:
            return ("CLAUDE", "A long and careful answer about caching.")

    def _live(self) -> tuple[SessionManager, Speaking, list[tuple[str, str]], SinkTransport]:
        transport = SinkTransport()
        asked: list[tuple[str, str]] = []
        session = self.Speaking()

        async def transform(instruction: str, material: str) -> str:
            asked.append((instruction, material))
            return "shorter version"

        async def save(_who: str | None) -> str:
            return "saved"

        manager = SessionManager(
            HudWriter(transport),
            transform=transform,
            save_reply=save,
        )
        assert manager.try_start_session(session)
        return manager, session, asked, transport

    async def test_an_open_instruction_acts_on_the_last_reply(self) -> None:
        manager, session, asked, transport = self._live()
        await asyncio.sleep(0)

        assert manager.handle_transcript(
            "JOAT, make that shorter", addressed="make that shorter",
        )
        for _ in range(40):
            await asyncio.sleep(0.01)
            if asked:
                break

        assert asked == [("make that shorter", "A long and careful answer about caching.")]
        assert session.consumed == []
        overlays = [m for m in transport.sent if isinstance(m, AssistantOverlay)]
        assert overlays and overlays[-1].text == "shorter version"
        await manager.close(timeout_s=0.05)

    async def test_many_different_verbs_all_work(self) -> None:
        manager, _s, asked, _t = self._live()
        await asyncio.sleep(0)
        for said in (
            "turn that into a spec",
            "explain the last reply",
            "summarise that",
            "rewrite it as bullet points",
            "compare that to what codex said",
        ):
            manager.handle_transcript(f"JOAT, {said}", addressed=said)
        for _ in range(60):
            await asyncio.sleep(0.01)
            if len(asked) == 5:
                break
        assert [a for a, _m in asked] == [
            "turn that into a spec",
            "explain the last reply",
            "summarise that",
            "rewrite it as bullet points",
            "compare that to what codex said",
        ]
        await manager.close(timeout_s=0.05)

    async def test_a_remark_is_still_a_remark(self) -> None:
        manager, session, asked, _t = self._live()
        await asyncio.sleep(0)
        for said in (
            "that seems short",
            "I think the cache is the wrong layer",
            "that is exactly my point",
        ):
            manager.handle_transcript(f"JOAT, {said}", addressed=said)
            await asyncio.sleep(0)
        assert asked == []
        assert session.consumed == [
            "that seems short",
            "I think the cache is the wrong layer",
            "that is exactly my point",
        ]
        await manager.close(timeout_s=0.05)


class TestInstructionsOutrankAbsorption:
    class Absorbing(Session):
        def __init__(self) -> None:
            self.consumed: list[str] = []

        async def run(self) -> None:
            await asyncio.Event().wait()

        def cancel(self) -> None:
            pass

        def consume_transcript(self, text: str) -> bool:
            self.consumed.append(text)
            return True

        def direct(self, text: str) -> str | None:
            return None

        def last_reply(self) -> tuple[str, str]:
            return ("CODEX", "cache on the session id")

    async def test_routing_survives_a_session_that_absorbs_everything(self) -> None:
        routed: list[tuple[str | None, str | None]] = []

        async def route(who: str | None, lane: str | None) -> str:
            routed.append((who, lane))
            return "sent"

        session = self.Absorbing()
        manager = SessionManager(HudWriter(SinkTransport()), route_reply=route)
        assert manager.try_start_session(session)
        await asyncio.sleep(0)

        assert manager.handle_transcript(
            "JOAT, send Claude's last reply to Codex",
            addressed="send Claude's last reply to Codex",
        )
        await asyncio.sleep(0)

        assert routed == [("claude", "codex")]
        assert session.consumed == []
        await manager.close(timeout_s=0.05)

    async def test_saving_survives_it_too(self) -> None:
        saved: list[str | None] = []

        async def save(who: str | None) -> str:
            saved.append(who)
            return "saved"

        session = self.Absorbing()
        manager = SessionManager(HudWriter(SinkTransport()), save_reply=save)
        assert manager.try_start_session(session)
        await asyncio.sleep(0)

        assert manager.handle_transcript("JOAT, note that", addressed="note that")
        await asyncio.sleep(0)
        assert saved == [None]
        assert session.consumed == []
        await manager.close(timeout_s=0.05)


class TestTheOrchestratorsActualCallShape:
    def _idle(self) -> tuple[SessionManager, list[tuple[Any, ...]]]:
        acted: list[tuple[Any, ...]] = []

        async def transform(instruction: str, material: str) -> str:
            acted.append(("transform", instruction))
            return "shorter"

        async def route(who: str | None, lane: str | None) -> str:
            acted.append(("route", who, lane))
            return "sent"

        manager = SessionManager(
            HudWriter(SinkTransport()),
            transform=transform,
            route_reply=route,
            save_reply=lambda _who: _noop(),
            last_reply=lambda _who: ("CLAUDE", "the settled answer"),
        )
        return manager, acted

    async def test_an_instruction_works_with_no_addressed_keyword(self) -> None:
        manager, acted = self._idle()
        assert manager.handle_transcript("make that shorter") is True
        await asyncio.sleep(0)
        assert acted == [("transform", "make that shorter")]
        await manager.close()

    async def test_routing_works_with_no_addressed_keyword(self) -> None:
        manager, acted = self._idle()
        assert manager.handle_transcript("send that to codex") is True
        await asyncio.sleep(0)
        assert acted == [("route", None, "codex")]
        await manager.close()

    async def test_explicitly_unaddressed_speech_is_still_ignored(self) -> None:
        manager, acted = self._idle()
        assert manager.handle_transcript("make that shorter", addressed=None) is False
        await asyncio.sleep(0)
        assert acted == []
        await manager.close()


class TestTheEscapeHatchHearsRealEndings:
    class Live(Session):
        def __init__(self) -> None:
            self.cancelled = False

        async def run(self) -> None:
            while not self.cancelled:
                await asyncio.sleep(0.01)

        def cancel(self) -> None:
            self.cancelled = True

        def consume_transcript(self, text: str) -> bool:
            return True

        def direct(self, text: str) -> str | None:
            return None

    async def _cancels(self, said: str) -> bool:
        live = self.Live()
        manager = SessionManager(HudWriter(SinkTransport()))
        assert manager.try_start_session(live)
        await asyncio.sleep(0)
        manager.handle_transcript(said)
        await asyncio.sleep(0.02)
        out = live.cancelled
        live.cancelled = True
        await manager.close()
        return out

    async def test_the_endings_people_actually_say(self) -> None:
        for said in (
            "Stop.", "Cancel.", "Never mind.",
            "That's enough.", "That’s enough.", "We're done.",
            "Forget it.", "Abort.", "Quit.", "Enough.", "Done.",
            "End it.", "JOAT, stop.",
        ):
            assert await self._cancels(said), said

    async def test_a_sentence_merely_containing_one_does_not_cancel(self) -> None:
        for said in (
            "that's enough sugar",
            "I want to stop the cache from growing",
            "we are done with the first round but not the session",
            "the abort path is broken",
        ):
            assert not await self._cancels(said), said


class TestTheInstructionVocabularies:
    def test_the_verb_layer_hears_ordinary_edits(self) -> None:
        from joat.services.session import _MATERIAL_VERB

        for said in (
            "make that shorter", "rewrite that as bullets",
            "summarise that", "turn that into a spec",
            "explain the last reply", "tighten that up",
            "fix the grammar in that", "clean that up",
            "polish that", "put that in plain english",
            "give me the gist of that",
        ):
            assert _MATERIAL_VERB.match(said), said

    def test_the_verb_layer_still_ignores_remarks(self) -> None:
        from joat.services.session import _MATERIAL_VERB

        for said in (
            "that seems short", "I think that is wrong",
            "the cache is the wrong layer", "make me a sandwich",
        ):
            assert not _MATERIAL_VERB.match(said), said

    def test_that_as_a_conjunction_is_a_remark_not_material(self) -> None:
        from joat.services.session import _MATERIAL_VERB

        for said in (
            "explain that the cache is stale",
            "check that the numbers add up",
            "show that we considered the alternative",
            "make that list of requirements the priority",
        ):
            assert not _MATERIAL_VERB.match(said), said

    def test_saving_hears_ordinary_phrasings(self) -> None:
        import re

        from joat.services.session import _SAVE_REPLY

        def norm(t: str) -> str:
            return " ".join(re.sub(r"[^a-z0-9]+", " ", t.lower()).split())

        for said in (
            "note that", "save that", "keep that", "remember that",
            "write that down", "jot that down", "record that",
            "add that to my notes", "save Claude's last reply",
            "save Antigravity's last reply",
        ):
            assert _SAVE_REPLY.match(norm(said)), said

    def test_saving_ignores_ordinary_speech(self) -> None:
        import re

        from joat.services.session import _SAVE_REPLY

        def norm(t: str) -> str:
            return " ".join(re.sub(r"[^a-z0-9]+", " ", t.lower()).split())

        for said in (
            "write the tests first", "add a cache layer",
            "note the difference between them", "keep going",
        ):
            assert not _SAVE_REPLY.match(norm(said)), said


class TestEveryVerbTellsTheWearerWhatHappened:
    @staticmethod
    def _manager(
        transport: SinkTransport,
        *,
        reply: Any,
        route: Any = None,
        transform: Any = None,
        save: Any = None,
    ) -> SessionManager:
        async def default_route(who: str | None, lane: str | None) -> str:
            return "sent."

        async def default_transform(instruction: str, material: str) -> str:
            return "a shorter version"

        async def default_save(who: str | None) -> str:
            return "saved to QuickNote"

        return SessionManager(
            HudWriter(transport),
            route_reply=route or default_route,
            transform=transform or default_transform,
            save_reply=save or default_save,
            last_reply=reply,
        )

    @staticmethod
    def _spoken(transport: SinkTransport) -> str:
        said = [
            m.text for m in transport.sent
            if isinstance(m, AssistantOverlay) and m.text
        ]
        return said[-1] if said else ""

    async def _run(self, said: str, **kw: Any) -> str:
        transport = SinkTransport()
        manager = self._manager(transport, **kw)
        manager.handle_transcript(said, addressed=said)
        await asyncio.sleep(0.05)
        spoken = self._spoken(transport)
        await manager.close()
        return spoken

    _HAVE = staticmethod(lambda _who: ("CLAUDE", "the answer"))
    _NONE = staticmethod(lambda _who: None)

    async def test_success_is_confirmed_for_every_verb(self) -> None:
        for said in ("send that to codex", "save that", "make that shorter"):
            spoken = await self._run(said, reply=self._HAVE)
            assert spoken, f"{said!r} succeeded silently"

    async def test_a_verb_with_no_material_says_so(self) -> None:
        for said in ("send that to codex", "make that shorter"):
            spoken = await self._run(said, reply=self._NONE)
            assert spoken, f"{said!r} failed silently"

    async def test_a_failing_verb_names_the_reason(self) -> None:
        async def boom(instruction: str, material: str) -> str:
            raise RuntimeError("the local model is not loaded")

        spoken = await self._run(
            "make that shorter", reply=self._HAVE, transform=boom,
        )
        assert "the local model is not loaded" in spoken

    async def test_a_routing_failure_reaches_the_glass(self) -> None:
        async def refuse(who: str | None, lane: str | None) -> str:
            raise RuntimeError("gateway said no")

        spoken = await self._run(
            "send that to codex", reply=self._HAVE, route=refuse,
        )
        assert "gateway said no" in spoken

    async def test_a_save_failure_reaches_the_glass(self) -> None:
        async def refuse(who: str | None) -> str:
            raise RuntimeError("the notes store is read-only")

        spoken = await self._run("save that", reply=self._HAVE, save=refuse)
        assert "read-only" in spoken

    async def test_the_receipt_is_marked_as_answering_the_wearer(self) -> None:
        transport = SinkTransport()
        manager = self._manager(transport, reply=self._HAVE)
        manager.handle_transcript(
            "send that to codex", addressed="send that to codex",
        )
        await asyncio.sleep(0.05)
        overlays = [
            m for m in transport.sent
            if isinstance(m, AssistantOverlay) and m.text
        ]
        assert overlays and all(m.answers_wearer for m in overlays)
        await manager.close()


class TestEveryGlassActionTellsTheWearerWhatHappened:
    @staticmethod
    def _spoken(transport: SinkTransport) -> str:
        said = [
            m.text for m in transport.sent
            if isinstance(m, AssistantOverlay) and m.text
        ]
        return said[-1] if said else ""

    async def _act(self, action: str, **kw: Any) -> str:
        transport = SinkTransport()
        manager = SessionManager(HudWriter(transport), **kw)
        manager.handle_terminal_action(TerminalAction(action=action))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        spoken = self._spoken(transport)
        await manager.close()
        return spoken

    async def test_an_answer_recorded_as_asked_is_confirmed(self) -> None:
        async def decide(verdict: str) -> str:
            return "approve"

        assert "approve" in await self._act(
            "approve_terminal", terminal_decide=decide,
        )

    async def test_being_overruled_by_another_controller_is_reported(self) -> None:
        async def decide(verdict: str) -> str:
            return "deny"

        spoken = await self._act("approve_terminal", terminal_decide=decide)
        assert "deny" in spoken
        assert "first" in spoken.lower()

    async def test_a_missing_receipt_is_not_dressed_up_as_success(self) -> None:
        async def decide(verdict: str) -> str:
            return ""

        assert "no receipt" in (
            await self._act("approve_terminal", terminal_decide=decide)
        ).lower()

    async def test_a_refused_answer_names_the_reason(self) -> None:
        async def decide(verdict: str) -> str:
            raise RuntimeError("gateway refused")

        assert "gateway refused" in await self._act(
            "approve_terminal", terminal_decide=decide,
        )

    async def test_a_failed_retry_is_caught_and_reported(self) -> None:
        async def retry() -> None:
            raise RuntimeError("codex is down")

        assert "codex is down" in await self._act(
            "retry_terminal", terminal_retry=retry,
        )

    async def test_a_failed_cancel_is_caught_and_reported(self) -> None:
        async def cancel() -> str:
            raise RuntimeError("socket closed")

        assert "socket closed" in await self._act(
            "cancel_terminal", terminal_cancel=cancel,
        )


class TestTerminalCapturePublicationBoundary:
    async def test_home_before_queued_publication_cannot_create_a_new_terminal_owner(self) -> None:
        from joat.services.agent_terminal import (
            AgentTerminalCommands,
            AgentTerminalRoster,
            TerminalCapture,
        )

        sent: list[str] = []

        class CapturedAgent:
            capture_context_generation = 1

            async def stream(self, prompt: str) -> AsyncIterator[str]:
                sent.append(prompt)
                yield "done"

            async def stream_captured(self, prompt: str, *, context_generation: int) -> AsyncIterator[str]:
                assert context_generation == self.capture_context_generation
                async for chunk in self.stream(prompt):
                    yield chunk

        roster = AgentTerminalRoster([("work", CapturedAgent())])
        commands = AgentTerminalCommands(roster)
        created: list[TerminalCapture | None] = []
        finished = asyncio.Event()

        def arm() -> Awaitable[None]:
            capture = commands.begin_capture()
            created.append(capture)

            async def publish() -> None:
                try:
                    if capture is not None and commands.capture_is_current(capture):
                        await commands.consume_capture(capture, "instruction for the old tap")
                finally:
                    finished.set()

            return publish()

        manager = SessionManager(
            HudWriter(SinkTransport()), terminal_capture=arm,
        )
        try:
            manager.handle_terminal_action(TerminalAction("begin_terminal_input"))
            owner_at_tap_return = commands.current_capture
            commands.clear_capture()
            await asyncio.wait_for(finished.wait(), 1)
            tasks = [lane.task for lane in roster._lanes.values() if lane.task is not None]
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 1)
            assert sent == [], "Publication created a fresh terminal owner after Home"
            assert owner_at_tap_return is not None and created == [owner_at_tap_return]
            assert commands.current_capture is None
        finally:
            await manager.close()
            for lane in roster._lanes.values():
                if lane.task is not None and not lane.task.done():
                    lane.task.cancel()
            tasks = [lane.task for lane in roster._lanes.values() if lane.task is not None]
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
            await asyncio.sleep(0)

    async def test_closed_manager_never_invokes_terminal_capture_callback(self) -> None:
        called: list[str] = []

        def arm() -> Awaitable[None]:
            called.append("new owner")

            async def publish() -> None:
                called.append("published")

            return publish()

        manager = SessionManager(
            HudWriter(SinkTransport()), terminal_capture=arm,
        )
        try:
            await manager.close()
            manager.handle_terminal_action(TerminalAction("begin_terminal_input"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert called == [], "Closed SessionManager created a terminal owner"
        finally:
            await manager.close()

    async def test_existing_async_terminal_callback_is_still_awaited(self) -> None:
        called: list[str] = []
        finished = asyncio.Event()

        async def arm() -> None:
            called.append("async callback")
            await asyncio.sleep(0)
            finished.set()

        manager = SessionManager(
            HudWriter(SinkTransport()), terminal_capture=arm,
        )
        try:
            manager.handle_terminal_action(TerminalAction("begin_terminal_input"))
            await asyncio.wait_for(finished.wait(), 1)
            assert called == ["async callback"]
        finally:
            await manager.close()

    @pytest.mark.parametrize("terminal_visible", [True, False])
    async def test_active_session_accepts_terminal_capture_only_while_terminal_owns_hud(
        self, terminal_visible: bool,
    ) -> None:
        class HeldSession(Session):
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.cancelled = False

            async def run(self) -> None:
                self.started.set()
                await self.release.wait()

            def cancel(self) -> None:
                self.cancelled = True
                self.release.set()

            def terminal_owns_hud(self) -> bool:
                return terminal_visible

        session = HeldSession()
        created: list[object] = []
        publication_started = asyncio.Event()
        publication_release = asyncio.Event()
        publication_finished = asyncio.Event()

        def arm() -> Awaitable[None]:
            created.append(object())

            async def publish() -> None:
                publication_started.set()
                try:
                    await publication_release.wait()
                finally:
                    publication_finished.set()

            return publish()

        manager = SessionManager(
            HudWriter(SinkTransport()), terminal_capture=arm,
        )
        try:
            assert manager.try_start_session(session)
            await asyncio.wait_for(session.started.wait(), 1)
            assert manager.terminal_owns_hud() is terminal_visible

            manager.handle_terminal_action(TerminalAction("begin_terminal_input"))

            assert len(created) == int(terminal_visible)
            if terminal_visible:
                await asyncio.wait_for(publication_started.wait(), 1)
            else:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert created == [] and not publication_started.is_set()
            assert manager.active_session is session and not session.cancelled
            assert not session.release.is_set()

            publication_release.set()
            if terminal_visible:
                await asyncio.wait_for(publication_finished.wait(), 1)
            assert manager.active_session is session and not session.cancelled
        finally:
            publication_release.set()
            await manager.close()
            if publication_started.is_set():
                await asyncio.wait_for(publication_finished.wait(), 1)


async def test_explicit_terminal_visit_owns_capture_during_active_session() -> None:
    selected = True
    called: list[str] = []

    async def capture() -> None:
        called.append("capture")

    manager = SessionManager(
        HudWriter(SinkTransport()),
        terminal_capture=capture,
        terminal_view_current=lambda: selected,
    )

    class Active:
        def terminal_owns_hud(self) -> bool:
            return False

    manager._active = Active()  # type: ignore[assignment]
    assert manager.terminal_owns_hud() is True
    selected = False
    assert manager.terminal_owns_hud() is False
    manager._active = None
    await manager.close()


@pytest.mark.parametrize("action", ["retry_terminal", "cancel_terminal"])
async def test_terminal_retry_and_cancel_bind_before_scheduled_action(action: str) -> None:
    selected = "work"
    captured: list[str] = []
    finished = asyncio.Event()

    def bind() -> Awaitable[Any]:
        target = selected
        captured.append(target)

        async def run() -> str | None:
            assert target == "work", "A queued action changed its target"
            finished.set()
            return "" if action == "cancel_terminal" else None

        return run()

    manager = SessionManager(
        HudWriter(SinkTransport()),
        terminal_retry=bind if action == "retry_terminal" else None,
        terminal_cancel=bind if action == "cancel_terminal" else None,
    )
    try:
        manager.handle_terminal_action(TerminalAction(action))  # type: ignore[arg-type]
        assert captured == ["work"]
        selected = "calendar"
        await asyncio.wait_for(finished.wait(), 1)
    finally:
        await manager.close()


def test_notification_stack_is_newest_first_with_the_whole_message() -> None:
    old = Card(source="Ann", channel="Mail", summary="Lunch?", body="Lunch on Friday at noon?", at=100.0)
    new = Card(source="Evan", channel="Messages", summary="On my way", at=200.0, urgency=Urgency.URGENT)
    wire = notification_stack([old, new]).to_wire()
    assert [card["sender"] for card in wire["cards"]] == ["Evan", "Ann"]
    assert wire["cards"][1]["body"] == "Lunch on Friday at noon?"
    assert wire["cards"][0]["body"] == "On my way"
    assert wire["cards"][0]["urgent"] is True
    assert wire["cards"][0]["at"] == 200_000
