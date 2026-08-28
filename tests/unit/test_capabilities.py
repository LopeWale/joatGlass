"""CapabilityRouter: the on-glasses menu's data views.

The wearer's hardware report that motivated this: double-tap opened 'a menu
modal, but not the one we designed'. The plugin now shows the designed rows;
this router is the half that draws each view.
"""

from __future__ import annotations

import asyncio
import time

from joat.channels.cards import CardLog
from joat.core.protocol import ContainerId, HudList, HudText, InputEvent
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.status import StatusPublisher


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, message: object) -> None:
        self.sent.append(message)


class FakeMessages:
    available = True

    def __init__(self, rows: list[tuple[str, str, float]]) -> None:
        self._rows = rows

    def recent(self, limit: int = 5) -> list[tuple[str, str, float]]:
        return self._rows[:limit]


def make_router(tmp_path, rows=None, cards=None):  # type: ignore[no-untyped-def]
    transport = FakeTransport()
    status = StatusPublisher(path=tmp_path / "status.json")
    log = CardLog()
    for c in cards or []:
        log.add(c, at=1.0)
    router = CapabilityRouter(
        hud=HudWriter(transport),  # type: ignore[arg-type]
        transport=transport,  # type: ignore[arg-type]
        status=status,
        messages=FakeMessages(rows) if rows is not None else None,  # type: ignore[arg-type]
        cards=log,
    )
    return router, transport, status


class TestCapabilityViews:
    async def test_messages_view_lists_recent_conversations(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        now = time.time()
        router, transport, _ = make_router(
            tmp_path, rows=[("Dele", "On my way", now), ("Adaeze", "Call me", now)])
        await router.open("Messages")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists and lists[0].to_wire()["items"] == [
            "[now] Dele · On my way", "[now] Adaeze · Call me"]
        assert lists[0].title == "MESSAGES"
        assert lists[0].hint == "left x2 overview"

    async def test_messages_without_access_states_the_gap(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path, rows=None)
        await router.open("Messages")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert any("Full Disk Access" in item for item in lists[0].items)
        assert lists[0].title == "MESSAGES · SETUP"
        # The focus title owns capability state. A separate status message can
        # arrive after Back and stale Home for twelve seconds even when the
        # plugin correctly rejects the delayed list response.
        statuses = [m for m in transport.sent
                    if isinstance(m, HudText)
                    and m.container_id is ContainerId.STATUS_BAR]
        assert statuses == []

    async def test_notifications_view_lists_recent_cards(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path, cards=["!! Adaeze · Messages\nCall me now"])
        await router.open("Notifications")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists and "!! Adaeze · Messages" in lists[0].to_wire()["items"][0]
        assert lists[0].title == "NOTIFICATIONS"

    async def test_calendar_admits_it_is_not_connected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)
        await router.open("Calendar")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert any("not connected" in item for item in lists[0].items)
        assert lists[0].title == "CALENDAR"


class TestGestureReflexes:
    async def test_menu_closed_repaints_the_last_exchange(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, status = make_router(tmp_path)
        status.set(last_reply="It is 72 degrees.", last_transcript="what is the weather")
        router.on_input(InputEvent(container_name="menu", selected_name="closed"))
        await asyncio.sleep(0.05)
        main = [m for m in transport.sent
                if isinstance(m, HudText) and m.container_id is ContainerId.MAIN]
        ctx = [m for m in transport.sent
               if isinstance(m, HudText) and m.container_id is ContainerId.CONTEXT]
        assert [m.text for m in main] == ["It is 72 degrees."]
        assert [m.text for m in ctx] == ["you  what is the weather"]

    async def test_repeat_with_no_history_says_so(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)
        router.on_input(InputEvent(container_name="sysmenu", selected_index=1))
        await asyncio.sleep(0.05)
        texts = [m for m in transport.sent if isinstance(m, HudText)]
        assert any("Nothing to repeat" in m.text for m in texts)
