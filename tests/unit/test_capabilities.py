"""CapabilityRouter: the on-glasses menu's data views.

The wearer's hardware report that motivated this: double-tap opened 'a menu
modal, but not the one we designed'. The plugin now shows the designed rows;
this router is the half that draws each view.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from joat.channels.cards import Card, CardLog, Urgency
from joat.channels.imessage import MessageEntry, MessageHistoryPage, MessageThread
from joat.core.protocol import (
    AgentTerminalHudView,
    VOICE_REQUEST_PREFIX,
    AssistantOverlay,
    BrainstormHudView,
    ContainerId,
    EventType,
    HudList,
    HudText,
    InputEvent,
)
from joat.native.bridge import NativeBridgeProtocolError, NativeBridgeUnavailable
from joat.persistence.actions import ActionDraft, ActionJournal
from joat.persistence.notes import NoteSource, NoteStore
from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import VoiceNoteStore
from joat.services.capabilities import CapabilityOpenController, CapabilityRouter
from joat.services.voice_note_capture import VoiceNoteCaptureSnapshot
from joat.services.hud import HudWriter
from joat.services.planning import PlanningScope
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

    def recent_threads(self, limit: int = 5) -> list[MessageThread]:
        return [
            MessageThread(
                row[0],
                f"handle-{index}",
                row[1],
                row[2],
                row[3] if len(row) > 3 else True,
                unread=row[4] if len(row) > 4 else False,
                attachment_count=row[5] if len(row) > 5 else 0,
            )
            for index, row in enumerate(self._rows[:limit])
        ]

    def thread_messages(
        self, thread: MessageThread, limit: int = 5,
    ) -> list[MessageEntry]:
        del limit
        return [MessageEntry(thread.text, thread.at, False)]

    def thread_message_page(
        self,
        thread: MessageThread,
        *,
        limit: int = 8,
        before_row_id: int | None = None,
    ) -> MessageHistoryPage:
        del limit, before_row_id
        return MessageHistoryPage(
            entries=(MessageEntry(thread.text, thread.at, False, row_id=1),),
            has_older=False,
        )


class FakeReplies:
    def __init__(self) -> None:
        self.selected: list[tuple[MessageThread, str]] = []
        self.composing: list[str] = []
        self.cleared: list[str] = []

    def select(self, thread: MessageThread, trace_id: str) -> None:
        self.selected.append((thread, trace_id))

    def begin_compose(self, trace_id: str) -> None:
        self.composing.append(trace_id)

    def clear_selection(self, reason: str = "explicit") -> None:
        self.cleared.append(reason)


class BlockingMessages:
    available = True

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def recent_threads(self, limit: int = 5) -> list[MessageThread]:
        del limit
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            self.release.wait(timeout=2)
            return [MessageThread("Old", "old", "stale", time.time())]
        return [MessageThread("New", "new", "current", time.time())]

    def thread_messages(
        self, thread: MessageThread, limit: int = 5,
    ) -> list[MessageEntry]:
        del limit
        return [MessageEntry(thread.text, thread.at, False)]

    def thread_message_page(
        self,
        thread: MessageThread,
        *,
        limit: int = 8,
        before_row_id: int | None = None,
    ) -> MessageHistoryPage:
        del limit, before_row_id
        return MessageHistoryPage(
            entries=(MessageEntry(thread.text, thread.at, False, row_id=1),),
            has_older=False,
        )


class PermissionDeniedMessages:
    available = True

    def recent_threads(self, limit: int = 5) -> list[MessageThread]:
        del limit
        raise PermissionError("Messages database read denied")


class TransientDatabaseMessages:
    available = True

    def recent_threads(self, limit: int = 5) -> list[MessageThread]:
        del limit
        raise sqlite3.DatabaseError("temporary database rotation")


class HistoryMessages(FakeMessages):
    def thread_messages(
        self, thread: MessageThread, limit: int = 5,
    ) -> list[MessageEntry]:
        del thread, limit
        return [
            MessageEntry("Are you coming?", 1.0, False),
            MessageEntry("On my way", 2.0, True),
            MessageEntry("Great", 3.0, False),
        ]

    def thread_message_page(
        self,
        thread: MessageThread,
        *,
        limit: int = 8,
        before_row_id: int | None = None,
    ) -> MessageHistoryPage:
        del thread, limit, before_row_id
        return MessageHistoryPage(entries=(
            MessageEntry("Great", 3.0, False, row_id=3),
            MessageEntry("On my way", 2.0, True, row_id=2),
            MessageEntry("Are you coming?", 1.0, False, row_id=1),
        ), has_older=False)


class LongHistoryMessages(FakeMessages):
    """One conversation with a text long enough to require reader paging."""

    body = "Opening words " + "continue through the full message " * 18

    def thread_message_page(
        self,
        thread: MessageThread,
        *,
        limit: int = 8,
        before_row_id: int | None = None,
    ) -> MessageHistoryPage:
        del thread, limit, before_row_id
        return MessageHistoryPage(entries=(
            MessageEntry(self.body, 3.0, False, row_id=3, unread=True),
        ), has_older=False)


class HistoryPermissionDeniedMessages(FakeMessages):
    def thread_messages(
        self, thread: MessageThread, limit: int = 5,
    ) -> list[MessageEntry]:
        del thread, limit
        raise PermissionError("Messages history read denied")

    def thread_message_page(
        self,
        thread: MessageThread,
        *,
        limit: int = 8,
        before_row_id: int | None = None,
    ) -> MessageHistoryPage:
        del thread, limit, before_row_id
        raise PermissionError("Messages history read denied")


class PagedHistoryMessages(FakeMessages):
    def __init__(self) -> None:
        super().__init__([("Dele", "Attachment", time.time())])
        self.page_calls: list[int | None] = []

    def thread_messages(
        self, thread: MessageThread, limit: int = 5,
    ) -> list[MessageEntry]:
        del thread, limit
        return [MessageEntry("legacy three-row context", 1.0, False)]

    def thread_message_page(
        self,
        thread: MessageThread,
        *,
        limit: int = 8,
        before_row_id: int | None = None,
    ) -> MessageHistoryPage:
        del thread, limit
        self.page_calls.append(before_row_id)
        if before_row_id is None:
            return MessageHistoryPage(entries=(
                MessageEntry(
                    "", 12.0, False, row_id=12, unread=True, attachment_count=1,
                ),
                MessageEntry(
                    "On my way", 11.0, True, row_id=11, delivery="read",
                ),
                MessageEntry("Are you coming?", 10.0, False, row_id=10),
            ), has_older=True)
        return MessageHistoryPage(entries=(
            MessageEntry("Earlier", 9.0, False, row_id=9),
        ), has_older=False)


class FakePersonalData:
    def __init__(self, responses):  # type: ignore[no-untyped-def]
        self._responses = responses
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def call(self, method: str, body=None):  # type: ignore[no-untyped-def]
        self.calls.append((method, body))
        response = self._responses[method]
        if isinstance(response, Exception):
            raise response
        return response


class StaleFailurePlanningData:
    """Delay an old authorization failure behind a newer successful page."""

    def __init__(self) -> None:
        self.first_authorization_started = asyncio.Event()
        self.release_first_authorization = asyncio.Event()
        self.authorization_calls = 0

    async def call(self, method: str, body=None):  # type: ignore[no-untyped-def]
        del body
        if method == "authorization.status":
            self.authorization_calls += 1
            if self.authorization_calls == 1:
                self.first_authorization_started.set()
                await self.release_first_authorization.wait()
                raise NativeBridgeUnavailable("old private socket failure")
            return bridge_response()
        return bridge_response(result=[])


def bridge_response(*, calendar="full_access", reminders="full_access", result=None):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        authorization=SimpleNamespace(calendar=calendar, reminders=reminders),
        result=result,
        store_instance_id="eventkit-store",
        store_generation=1,
    )


async def wait_until(predicate, *, attempts: int = 100) -> None:  # type: ignore[no-untyped-def]
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


def message_row_index(transport: FakeTransport, label: str) -> int:
    frame = [
        item for item in transport.sent
        if isinstance(item, HudList) and item.title == "MESSAGES"
    ][-1]
    return next(index for index, item in enumerate(frame.items) if label in item)


def _retained_voice_note(notes, capture_id, *, base_us):  # type: ignore[no-untyped-def]
    notes.create(
        capture_id=capture_id,
        idempotency_key=f"voice-note:{capture_id}",
        title="Standup notes",
        source_kind="g2.microphone",
        now_us=base_us,
    )
    notes.transition(capture_id, "recording", now_us=base_us + 1)
    notes.append_frame(
        capture_id,
        connection_generation="socket-a",
        seq=1,
        source="glasses",
        speaker="wearer",
        pcm=b"\x01\x00",
        received_at_us=base_us + 2,
    )
    notes.transition(capture_id, "finalizing", now_us=base_us + 3)
    notes.append_transcript(
        capture_id,
        text="We agreed to ship Friday.",
        first_frame_ordinal=0,
        last_frame_ordinal=0,
        now_us=base_us + 4,
    )
    notes.finalize(
        capture_id,
        complete=True,
        completion_code="transcript_checkpointed_raw_purged",
        now_us=base_us + 5,
    )


class FakeVoiceCapture:
    """Duck-typed VoiceCaptureControls with scriptable live state."""

    def __init__(
        self,
        *,
        state: str = "recording",
        title: str = "Untitled",
        discard_pending: bool = False,
        active: bool = True,
    ) -> None:
        self._state = state
        self._title = title
        self._discard = discard_pending
        self._active = active
        self.calls: list[str] = []

    @property
    def active(self) -> bool:
        return self._active

    @property
    def discard_pending(self) -> bool:
        return self._discard

    @property
    def snapshot(self) -> VoiceNoteCaptureSnapshot:
        return VoiceNoteCaptureSnapshot(
            capture_id="capture-live",
            title=self._title,
            state=self._state,
            received_frames=0,
            journaled_frames=0,
            gap_count=0,
            failure_code=None,
        )

    def control_pause(self) -> str:
        self.calls.append("pause")
        self._state = "paused"
        return "Paused Voice Note."

    def control_resume(self) -> str:
        self.calls.append("resume")
        self._state = "recording"
        return "Resumed Voice Note."

    def control_request_discard(self) -> str:
        self.calls.append("discard")
        self._discard = True
        return "Discard Voice Note?"

    def control_confirm_discard(self) -> str:
        self.calls.append("confirm_discard")
        self._discard = False
        self._active = False
        return "Discarded Voice Note."

    def control_keep(self) -> str:
        self.calls.append("keep")
        self._discard = False
        return "Kept Voice Note."

    async def control_stop(self) -> str:
        self.calls.append("stop")
        self._active = False
        return "Saved Voice Note: fixture."


def _urgent_card() -> Card:
    """Renders as "!! Adaeze · Messages\\nCall me now" — the prefix and the
    two-line shape come from the card, not from hand-written text."""
    return Card(source="Adaeze", channel="Messages", summary="Call me now",
                urgency=Urgency.URGENT, event_class="messages.incoming_text",
                at=1.0)


def make_router(
    tmp_path,
    rows=None,
    cards=None,
    replies=None,
    personal_data=None,
    notes=None,
    voice_notes=None,
    voice_capture=None,
    actions=None,
    acceptance_armed=False,
    brainstorm_recap=None,
    brainstorm_replay=None,
    brainstorm_sessions=None,
    brainstorm_continue=None,
    terminal_rows=None,
    terminal_focus=None,
    terminal_view=None,
    terminal_busy=None,
):  # type: ignore[no-untyped-def]
    transport = FakeTransport()
    status = StatusPublisher(path=tmp_path / "status.json")
    log = CardLog()
    for c in cards or []:
        log.add(c)
    router = CapabilityRouter(
        hud=HudWriter(transport),  # type: ignore[arg-type]
        transport=transport,  # type: ignore[arg-type]
        status=status,
        messages=FakeMessages(rows) if rows is not None else None,  # type: ignore[arg-type]
        cards=log,
        replies=replies,  # type: ignore[arg-type]
        personal_data=personal_data,  # type: ignore[arg-type]
        notes=notes,
        voice_notes=voice_notes,
        voice_capture=voice_capture,
        actions=actions,
        acceptance_armed=acceptance_armed,
        brainstorm_recap=brainstorm_recap,
        brainstorm_replay=brainstorm_replay,
        brainstorm_sessions=brainstorm_sessions,
        brainstorm_continue=brainstorm_continue,
        terminal_rows=terminal_rows,
        terminal_focus=terminal_focus,
        terminal_view=terminal_view,
        terminal_busy=terminal_busy,
    )
    return router, transport, status


class TestCapabilityViews:
    async def test_brainstorm_and_agent_terminal_open_real_hud_views(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, status = make_router(tmp_path)
        status.set_openclaw(
            state="connected_read_only",
            endpoint_host="home-mac.example-tailnet.ts.net",
            scopes=["operator.read"],
            reason="",
        )
        status.set_agent_terminal(state="ready", reason="")
        await router.open("Brainstorm")
        await router.open("Agent Terminal")
        # Two surfaces, two frame TYPES — the discriminator used to be a
        # `screen` field on one shared frame (SESSION_MODEL.md:74-75).
        frames = [item for item in transport.sent
                  if isinstance(item, (BrainstormHudView, AgentTerminalHudView))]
        assert [type(frame).__name__ for frame in frames] == [
            "BrainstormHudView", "AgentTerminalHudView",
        ]
        assert frames[-1].endpoint_host == "home-mac.example-tailnet.ts.net"
        assert frames[-1].no_tools_checked is True
        # acceptance_armed is a BRAINSTORM field; the terminal frame has no
        # such concept, which is the point of the split.
        assert frames[0].acceptance_armed is False

    @pytest.mark.parametrize(
        ("state", "reason", "expected"),
        [
            (
                "approval_required",
                "write_scope_required",
                "Approve the JOAT device in OpenClaw, then tap retry.",
            ),
            (
                "unavailable",
                "gateway_not_ready",
                "Hermes is unavailable; retry when the Mac connection recovers.",
            ),
            (
                "blocked",
                "tools_enabled",
                "Hermes tools are enabled; this JOAT session remains blocked.",
            ),
        ],
    )
    async def test_agent_terminal_projects_safe_recovery_copy_to_glasses(
        self, tmp_path, state, reason, expected,
    ) -> None:  # type: ignore[no-untyped-def]
        router, transport, status = make_router(tmp_path)
        status.set_agent_terminal(state=state, reason=reason)

        await router.open("Agent Terminal")

        frame = next(item for item in transport.sent
                     if isinstance(item, AgentTerminalHudView))
        assert frame.state == state
        assert reason not in frame.reason

    async def test_brainstorm_acceptance_requires_an_explicit_mac_diagnostics_arm(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        router, transport, status = make_router(tmp_path, acceptance_armed=True)
        status.set_agent_terminal(state="ready", reason="")

        await router.open("Brainstorm")

        frame = next(item for item in transport.sent
                     if isinstance(item, BrainstormHudView))
        assert frame.acceptance_armed is True

    async def test_messages_view_lists_recent_conversations(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        now = time.time()
        router, transport, _ = make_router(
            tmp_path, rows=[("Dele", "On my way", now), ("Adaeze", "Call me", now)])
        await router.open("Messages")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists and lists[0].to_wire()["items"] == [
            "New message · voice recipient + text",
            "[now] Dele · On my way",
            "[now] Adaeze · Call me",
        ]
        assert lists[0].title == "MESSAGES"
        assert lists[0].items[0] == "New message · voice recipient + text"
        assert lists[0].hint == "tap: select · left x2: back"

    async def test_messages_list_marks_unread_attachment_only_conversations(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)
        router._messages = FakeMessages([  # type: ignore[assignment]
            ("Dele", "", time.time(), True, True, 1),
        ])

        await router.open("Messages")

        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.items == [
            "New message · voice recipient + text",
            "[now] ● Dele · Attachment",
        ]

    async def test_selecting_new_message_opens_voice_compose_without_sending(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        router, transport, _ = make_router(
            tmp_path,
            rows=[("Dele", "On my way", time.time())],
            replies=replies,
        )
        await router.open("Messages", request_id="messages-compose")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "New message"),
            trace_id="hud-compose",
            request_id="messages-compose",
        ))
        await wait_until(lambda: bool(replies.composing))

        assert replies.composing == ["hud-compose"]
        assert replies.selected == []
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "MESSAGES · COMPOSE"
        assert detail.items == [
            "Say the recipient name first.",
            "Then say the message.",
            "One matching Mac contact is required.",
        ]
        assert detail.hint == "speak: recipient · left x2: back"
        assert detail.selectable is False

    async def test_new_message_remains_selectable_when_no_conversations_exist(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, rows=[], replies=replies)

        await router.open("Messages", request_id="messages-empty-compose")

        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "MESSAGES"
        assert detail.items == ["New message · voice recipient + text"]
        assert detail.selectable is True

    async def test_navigation_capability_explains_the_spoken_destination_handoff(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)

        await router.open("Navigation", request_id="navigation-entry")

        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert len(lists) == 1
        assert lists[0].title == "NAVIGATION"
        assert lists[0].items == [
            'Say “JOAT, navigate to …”',
            "I’ll confirm the place. Then choose Walk or Cycle.",
        ]
        assert lists[0].hint == "say destination · left x2: back"
        assert lists[0].request_id == "navigation-entry"

    async def test_navigation_entry_opens_from_the_real_capability_menu_input(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)

        router.on_input(InputEvent(
            container_name="capmenu",
            selected_name="Navigation",
            selected_index=2,
            request_id="navigation-menu-entry",
        ))
        await wait_until(lambda: any(
            isinstance(message, HudList) and message.title == "NAVIGATION"
            for message in transport.sent
        ))

        frame = [
            message for message in transport.sent
            if isinstance(message, HudList) and message.title == "NAVIGATION"
        ][-1]
        assert frame.items == [
            'Say “JOAT, navigate to …”',
            "I’ll confirm the place. Then choose Walk or Cycle.",
        ]
        assert frame.selectable is False
        assert frame.request_id == "navigation-menu-entry"

    async def test_newest_same_capability_generation_owns_render_and_selection(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        messages = BlockingMessages()
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, replies=replies)
        router._messages = messages  # type: ignore[assignment]

        stale = asyncio.create_task(router.open("Messages", request_id="request-old"))
        assert await asyncio.to_thread(messages.started.wait, 1)
        await router.open("Messages", request_id="request-new")
        messages.release.set()
        await stale

        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert len(lists) == 1
        assert lists[0].request_id == "request-new"
        assert lists[0].selectable is True
        assert "New" in lists[0].items[1]
        selected_row = message_row_index(transport, "] New ·")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=selected_row,
            trace_id="selection-old",
            request_id="request-old",
        ))
        await asyncio.sleep(0)
        assert replies.selected == []

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=selected_row,
            trace_id="selection-new",
            request_id="request-new",
        ))
        await wait_until(lambda: bool(replies.selected))
        assert replies.selected[0][0].sender == "New"
        assert replies.selected[0][1] == "selection-new"

    async def test_selecting_a_message_arms_voice_reply_without_sending(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        now = time.time()
        replies = FakeReplies()
        router, transport, _ = make_router(
            tmp_path,
            rows=[("Dele", "On my way", now), ("Adaeze", "Call me", now)],
            replies=replies,
        )
        await router.open("Messages")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Adaeze ·"),
            trace_id="hud-12",
        ))
        await wait_until(lambda: bool(replies.selected))

        assert replies.selected == [(
            MessageThread("Adaeze", "handle-1", "Call me", now),
            "hud-12",
        )]
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "MESSAGES · CONVERSATION"
        assert any("JOAT, reply" in item for item in detail.items)

    async def test_selecting_a_message_opens_complete_conversation_context(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, replies=replies)
        router._messages = HistoryMessages([  # type: ignore[assignment]
            ("Dele", "Great", time.time()),
        ])
        await router.open("Messages", request_id="messages-context")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Dele ·"),
            trace_id="hud-context",
            request_id="messages-context",
        ))
        await wait_until(lambda: any(
            isinstance(item, HudList) and item.title == "MESSAGES · CONVERSATION"
            for item in transport.sent
        ))

        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "MESSAGES · CONVERSATION"
        assert detail.items == [
            "Complete conversation on this Mac",
            "Dele · Great",
            "You · On my way",
            "Dele · Are you coming?",
            'Say “JOAT, reply saying …”',
            "Mic off? Long press to enable.",
        ]
        assert detail.hint == "tap read · say reply · left x2: back"
        assert detail.selectable is True

    async def test_selecting_a_transcript_entry_opens_the_paged_full_text_reader(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, replies=replies)
        router._messages = LongHistoryMessages([  # type: ignore[assignment]
            ("Dele", "Preview", time.time()),
        ])
        await router.open("Messages", request_id="messages-full-reader")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Dele ·"),
            trace_id="hud-reader-conversation",
            request_id="messages-full-reader",
        ))
        await wait_until(lambda: any(
            isinstance(item, HudList) and item.title == "MESSAGES · CONVERSATION"
            for item in transport.sent
        ))

        router.on_input(InputEvent(
            container_name="capmessage",
            selected_name="MESSAGES",
            selected_index=1,
            trace_id="hud-reader-open",
            request_id="messages-full-reader",
        ))
        await wait_until(lambda: any(
            isinstance(item, HudList) and item.title == "MESSAGES · MESSAGE"
            for item in transport.sent
        ))
        first_page = [
            item for item in transport.sent
            if isinstance(item, HudList) and item.title == "MESSAGES · MESSAGE"
        ][-1]
        assert first_page.items[0] == "From Dele · unread · 1/2"
        assert len(first_page.items) > 2
        assert all(len(line) <= 52 for line in first_page.items[1:])
        assert " ".join(first_page.items[1:]).startswith("Opening words")
        assert first_page.hint == "down more · say reply · left x2: back"
        assert first_page.selectable is False
        assert len(replies.selected) == 1

        router.on_input(InputEvent(
            container_name="cappage",
            selected_name="MESSAGES",
            event_type=EventType.SCROLL_BOTTOM,
            trace_id="hud-reader-more",
            request_id="messages-full-reader",
        ))
        await wait_until(lambda: len([
            item for item in transport.sent
            if isinstance(item, HudList) and item.title == "MESSAGES · MESSAGE"
        ]) == 2)
        second_page = [
            item for item in transport.sent
            if isinstance(item, HudList) and item.title == "MESSAGES · MESSAGE"
        ][-1]
        assert second_page.items[0] == "From Dele · unread · 2/2"
        assert second_page.hint == "up previous · say reply · left x2: back"
        assert first_page.items[1] != second_page.items[1]
        assert len(replies.selected) == 1

        router.on_input(InputEvent(
            container_name="capability-lifecycle",
            selected_name="messages-conversation",
        ))
        assert router._message_detail is None
        assert router._message_history is not None
        assert replies.cleared == []

        router.on_input(InputEvent(
            container_name="capability-lifecycle",
            selected_name="messages-home",
        ))
        assert router._message_history is None
        assert replies.cleared == ["home"]

    async def test_conversation_pages_are_request_bound_and_show_message_metadata(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        messages = PagedHistoryMessages()
        router, transport, _ = make_router(tmp_path, replies=replies)
        router._messages = messages  # type: ignore[assignment]
        await router.open("Messages", request_id="messages-history")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Dele ·"),
            trace_id="hud-history",
            request_id="messages-history",
        ))
        await wait_until(lambda: any(
            isinstance(item, HudList) and item.title == "MESSAGES · CONVERSATION"
            for item in transport.sent
        ))

        latest = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert latest.items == [
            "Latest messages — down for older",
            "Dele · Unread · Attachment",
            "You · Read · On my way",
            "Dele · Are you coming?",
            'Say “JOAT, reply saying …”',
            "Mic off? Long press to enable.",
        ]
        assert latest.hint == "tap read · say reply · left x2: back"

        router.on_input(InputEvent(
            container_name="cappage",
            selected_name="MESSAGES",
            event_type=2,
            trace_id="hud-history-older",
            request_id="messages-history",
        ))
        await wait_until(lambda: any(
            isinstance(item, HudList)
            and item.items
            and item.items[0] == "Older messages — up for newer"
            for item in transport.sent
        ))
        older = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert messages.page_calls == [None, 10]
        assert older.items == [
            "Older messages — up for newer",
            "Dele · Earlier",
            'Say “JOAT, reply saying …”',
            "Mic off? Long press to enable.",
        ]
        assert older.hint == "tap read · say reply · left x2: back"
        # The async page task clears its duplicate-input guard after its
        # replacement frame is committed, as the production bridge does.
        await asyncio.sleep(0)

        router.on_input(InputEvent(
            container_name="cappage",
            selected_name="MESSAGES",
            event_type=1,
            trace_id="hud-history-newer",
            request_id="messages-history",
        ))
        await wait_until(lambda: any(
            isinstance(item, HudList)
            and item.items
            and item.items[0] == "Latest messages — down for older"
            for item in transport.sent[2:]
        ))
        assert messages.page_calls == [None, 10]

        router.on_input(InputEvent(
            container_name="cappage",
            selected_name="MESSAGES",
            event_type=1,
            trace_id="stale-history-newer",
            request_id="stale-request",
        ))
        await asyncio.sleep(0)
        assert messages.page_calls == [None, 10]

    async def test_selection_time_permission_loss_replaces_the_list_with_retry(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, replies=replies)
        router._messages = HistoryPermissionDeniedMessages([  # type: ignore[assignment]
            ("Dele", "Great", time.time()),
        ])
        await router.open("Messages", request_id="messages-history-denied")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Dele ·"),
            trace_id="hud-history-denied",
            request_id="messages-history-denied",
        ))
        await wait_until(lambda: any(
            isinstance(item, HudList) and item.title == "MESSAGES · ACCESS"
            for item in transport.sent
        ))

        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.items[0] == "Tap to retry after granting Full Disk Access."
        assert detail.request_id == "messages-history-denied"
        assert replies.selected == []

    async def test_returning_home_clears_the_selected_message_recipient(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        router, transport, _ = make_router(
            tmp_path,
            rows=[("Dele", "On my way", time.time())],
            replies=replies,
        )
        await router.open("Messages", request_id="messages-home")
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Dele ·"),
            trace_id="hud-home",
            request_id="messages-home",
        ))
        await wait_until(lambda: bool(replies.selected))
        assert replies.selected

        router.on_input(InputEvent(
            container_name="capability-lifecycle",
            selected_name="messages-home",
        ))
        await asyncio.sleep(0)

        assert replies.cleared == ["home"]

    async def test_reopening_messages_clears_a_previously_selected_recipient(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """A fresh picker must never leave a reply target from an old visit armed."""
        replies = FakeReplies()
        router, transport, _ = make_router(
            tmp_path,
            rows=[("Dele", "On my way", time.time())],
            replies=replies,
        )
        await router.open("Messages", request_id="messages-first")
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Dele ·"),
            trace_id="hud-first-selection",
            request_id="messages-first",
        ))
        await wait_until(lambda: bool(replies.selected))

        await router.open("Messages", request_id="messages-second")

        assert replies.cleared == ["messages-open"]
        picker = [message for message in transport.sent if isinstance(message, HudList)][-1]
        assert picker.title == "MESSAGES"
        assert picker.request_id == "messages-second"
        assert picker.selectable is True

    async def test_disconnect_clears_the_selected_message_recipient(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        router, transport, _ = make_router(
            tmp_path,
            rows=[("Dele", "On my way", time.time())],
            replies=replies,
        )
        await router.open("Messages", request_id="messages-disconnect")
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Dele ·"),
            trace_id="hud-disconnect",
            request_id="messages-disconnect",
        ))
        await wait_until(lambda: bool(replies.selected))

        router.on_disconnect()

        assert replies.cleared == ["disconnect"]

    async def test_group_message_cannot_be_misrouted_to_one_participant(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        now = time.time()
        replies = FakeReplies()
        router, transport, _ = make_router(
            tmp_path,
            rows=[("Project team", "Ship it", now, False)],
            replies=replies,
        )
        await router.open("Messages")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Project team"),
            trace_id="hud-group",
        ))
        await asyncio.sleep(0)

        assert replies.selected == []
        # The refusal is a band over the conversation list, not a page that
        # replaces it: the list the wearer was reading is still the surface.
        assert [m.title for m in transport.sent if isinstance(m, HudList)][-1] == "MESSAGES"
        band = [m for m in transport.sent if isinstance(m, AssistantOverlay)]
        assert any(
            m.text is not None and "read-only" in m.text for m in band
        )

    async def test_messages_without_access_states_the_gap(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path, rows=None)
        await router.open("Messages")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists[0].title == "MESSAGES · SETUP"
        assert lists[0].items == [
            "Tap to retry after granting Full Disk Access.",
            "Mac Settings · Privacy & Security · Full Disk Access",
        ]
        # The focus title owns capability state. A separate status message can
        # arrive after Back and stale Home for twelve seconds even when the
        # plugin correctly rejects the delayed list response.
        statuses = [m for m in transport.sent
                    if isinstance(m, HudText)
                    and m.container_id is ContainerId.STATUS_BAR]
        assert statuses == []

    async def test_messages_permission_failure_replaces_loading_with_access_guidance(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path, rows=[])
        router._messages = PermissionDeniedMessages()  # type: ignore[assignment]

        raised = False
        try:
            await router.open("Messages", request_id="messages-denied")
        except PermissionError:
            raised = True

        assert raised is False
        rendered = [message for message in transport.sent if isinstance(message, HudList)][-1]
        assert rendered.title == "MESSAGES · ACCESS"
        assert rendered.items == [
            "Tap to retry after granting Full Disk Access.",
            "Mac Settings · Privacy & Security · Full Disk Access",
        ]
        assert rendered.request_id == "messages-denied"

    async def test_messages_transient_database_failure_is_a_retry_not_access_guidance(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path, rows=[])
        router._messages = TransientDatabaseMessages()  # type: ignore[assignment]

        await router.open("Messages", request_id="messages-transient")

        rendered = [message for message in transport.sent if isinstance(message, HudList)][-1]
        assert rendered.title == "MESSAGES · RETRY"
        assert rendered.items == [
            "Messages is temporarily unavailable.",
            "Tap to try Messages again.",
        ]
        assert rendered.selectable is True
        assert rendered.request_id == "messages-transient"

    async def test_notifications_view_lists_recent_cards(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path, cards=[_urgent_card()])
        await router.open("Notifications")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists and "!! Adaeze · Messages" in lists[0].to_wire()["items"][0]
        assert lists[0].title == "NOTIFICATIONS"

    async def test_notification_selection_opens_the_complete_two_line_card(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(
            tmp_path,
            cards=[_urgent_card()],
        )
        await router.open("Notifications")
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="NOTIFICATIONS",
            selected_index=0,
        ))
        await asyncio.sleep(0)

        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "NOTIFICATIONS · DETAIL"
        assert detail.items == ["!! Adaeze · Messages", "Call me now"]

    async def test_planning_directs_setup_to_the_explicit_mac_control(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)
        await router.open("Planning")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert any("Calendar and Reminders" in item for item in lists[0].items)
        assert lists[0].title == "PLANNING · PLAN · SETUP"

    async def test_planning_handles_a_malformed_native_bridge_response(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(
            tmp_path,
            personal_data=FakePersonalData({
                "authorization.status": NativeBridgeProtocolError("malformed response"),
            }),
        )

        await router.open("Planning")

        rendered = [message for message in transport.sent if isinstance(message, HudList)][-1]
        assert rendered.title == "PLANNING · PLAN · UNAVAILABLE"
        assert rendered.items == [
            "Calendar and Reminders bridge needs a JOAT update on the Mac.",
        ]

    async def test_planning_distinguishes_a_granted_access_from_bridge_loss(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        from joat.native.bridge import NativeBridgeUnavailable

        router, transport, status = make_router(
            tmp_path,
            personal_data=FakePersonalData({
                "authorization.status": NativeBridgeUnavailable("closed socket"),
            }),
        )

        await router.open("Planning")

        rendered = [message for message in transport.sent if isinstance(message, HudList)][-1]
        assert rendered.title == "PLANNING · PLAN · UNAVAILABLE"
        assert rendered.items == [
            "Calendar and Reminders bridge is unavailable. Restart JOAT on the Mac.",
        ]
        snapshot = json.loads((tmp_path / "status.json").read_text())
        assert snapshot["personal_data"] == {"state": "unavailable"}

    async def test_stale_planning_failure_cannot_overwrite_new_ready_bridge_state(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        personal = StaleFailurePlanningData()
        router, transport, _ = make_router(tmp_path, personal_data=personal)

        old = asyncio.create_task(router.open("Planning", request_id="planning-old"))
        await personal.first_authorization_started.wait()
        await router.open("Planning", request_id="planning-new")
        personal.release_first_authorization.set()
        await old

        snapshot = json.loads((tmp_path / "status.json").read_text())
        assert snapshot["personal_data"] == {"state": "ready"}
        rendered = [message for message in transport.sent if isinstance(message, HudList)]
        assert len(rendered) == 1
        assert rendered[0].request_id == "planning-new"

    async def test_planning_opens_one_native_backed_month_week_day_hierarchy(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        starts_at = datetime(2026, 9, 1, 9, 30).astimezone()
        personal = FakePersonalData({
            "authorization.status": bridge_response(),
            "calendar.listContainers": bridge_response(result=[
                {"calendar_identifier": "cal-work", "title": "Work"},
            ]),
            "calendar.query": bridge_response(result=[
                {
                    "title": "Design review",
                    "calendar_item_identifier": "event-1",
                    "start_unix_ms": int(starts_at.timestamp() * 1_000),
                    "end_unix_ms": int(starts_at.timestamp() * 1_000) + 3_600_000,
                },
            ]),
            "reminders.listContainers": bridge_response(result=[
                {"calendar_identifier": "list-home", "title": "Home"},
            ]),
            "reminders.query": bridge_response(result=[
                {
                    "title": "Buy filters",
                    "calendar_item_identifier": "reminder-1",
                    "due": {"year": 2026, "month": 9, "day": 1, "hour": 18, "minute": 0},
                    "is_completed": False,
                },
            ]),
        })
        router, transport, _ = make_router(tmp_path, personal_data=personal)
        router._planning_selected_day = date(2026, 9, 1)

        await router.open("Planning")

        calendar_query = next(call for call in personal.calls if call[0] == "calendar.query")
        reminders_query = next(call for call in personal.calls if call[0] == "reminders.query")
        assert calendar_query[1]["calendar_identifiers"] == ["cal-work"]
        assert calendar_query[1]["limit"] == 500
        assert reminders_query[1]["calendar_identifiers"] == ["list-home"]
        assert reminders_query[1]["include_completed"] is False
        assert reminders_query[1]["due_start_unix_ms"] < reminders_query[1]["due_end_unix_ms"]
        rendered = [m for m in transport.sent if isinstance(m, HudList)][0]
        assert rendered.title == "PLANNING · PLAN · SEPTEMBER 2026"
        assert rendered.items[0] == "AUG 31–SEP 6 · 2 planned"
        assert rendered.planning_grid is not None
        assert len(rendered.planning_grid) == 42
        september_first = next(cell for cell in rendered.planning_grid if cell.day == date(2026, 9, 1))
        assert september_first.event_count == 1
        assert september_first.reminder_count == 1
        assert september_first.in_month is True

        # G2 click 1: choose a month week; G2 click 2: choose Sep 1.
        router.on_input(InputEvent(container_name="capdata", selected_name="PLANNING", selected_index=0))
        await asyncio.sleep(0)
        week = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert week.title == "PLANNING · PLAN · AUG 31–SEP 6"
        assert week.items[1] == "TUE SEP 1 · 1 event · 1 reminder"
        assert week.planning_grid is None

        router.on_input(InputEvent(container_name="capdata", selected_name="PLANNING", selected_index=1))
        await asyncio.sleep(0)
        day = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert day.title == "PLANNING · PLAN · TUE SEP 1"
        assert day.items[:2] == [
            "[09:30] Design review",
            "[18:00] □ Buy filters",
        ]

        # A native item stays inside Planning; it is not a synthetic Calendar
        # detail page that can drift from the source-backed schedule.
        router.on_input(InputEvent(
            container_name="capdata", selected_name="PLANNING", selected_index=0
        ))
        await asyncio.sleep(0)
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "PLANNING · EVENT"
        assert detail.items == [
            "Design review",
            "09:30 · Event",
            "Read-only native item · left x2 Planning",
        ]
        assert detail.hint == "left x2 Planning"

        # Old installed Calendar/Tasks menu rows are compatibility aliases for
        # that same nested Plan, not separate source-specific lists.
        await router.open("Calendar")
        calendar = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert calendar.title == "CALENDAR · PLAN · TUE SEP 1"
        assert "[09:30] Design review" in calendar.items

        await router.open("Tasks")
        tasks = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert tasks.title == "TASKS · PLAN · TUE SEP 1"
        assert "[09:30] Design review" in tasks.items
        assert "[18:00] □ Buy filters" in tasks.items

        # A pre-Planning package could have opened the same projection under
        # ``Reminders``.  Its subsequent capdata selection must not become an
        # inert page merely because the router canonicalizes that name to
        # TASKS for its single native-backed hierarchy.
        router._planning_scope = PlanningScope.MONTH
        await router.open("Reminders")
        router.on_input(InputEvent(
            container_name="capdata", selected_name="REMINDERS", selected_index=0
        ))
        await asyncio.sleep(0)
        legacy_week = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert legacy_week.title == "TASKS · PLAN · AUG 31–SEP 6"
        assert legacy_week.items[1] == "TUE SEP 1 · 1 event · 1 reminder"

    async def test_planning_return_anchor_opens_the_native_day_not_today(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        starts_at = datetime(2026, 9, 1, 9, 30).astimezone()
        personal = FakePersonalData({
            "authorization.status": bridge_response(),
            "calendar.listContainers": bridge_response(result=[
                {"calendar_identifier": "cal-work", "title": "Work"},
            ]),
            "calendar.query": bridge_response(result=[{
                "title": "Design review",
                "calendar_item_identifier": "event-1",
                "start_unix_ms": int(starts_at.timestamp() * 1_000),
                "end_unix_ms": int(starts_at.timestamp() * 1_000) + 3_600_000,
            }]),
            "reminders.listContainers": bridge_response(result=[]),
            "reminders.query": bridge_response(result=[]),
        })
        router, transport, _ = make_router(tmp_path, personal_data=personal)

        await router.open(
            "Planning",
            planning_anchor_unix_ms=int(starts_at.timestamp() * 1_000),
        )

        rendered = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert rendered.title == "PLANNING · PLAN · TUE SEP 1"
        assert rendered.items[0] == "[09:30] Design review"

    async def test_quicknotes_and_activity_are_durable_reachable_views(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = DurableStore(tmp_path / "durable.sqlite3")
        notes = NoteStore(store)
        notes.create(
            note_id="note-1",
            idempotency_key="voice-1",
            text="Ask Adaeze about the design review",
            source=NoteSource(kind="voice.command"),
            now_us=10,
        )
        actions = ActionJournal(store)
        actions.create_draft(
            ActionDraft(
                kind="calendar.create",
                canonical_payload=b'{"title":"Design review"}',
                target_digest=b"t" * 32,
                idempotency_key="calendar-1",
            ),
            now_us=20,
        )
        router, transport, _ = make_router(
            tmp_path,
            notes=notes,
            actions=actions,
        )

        await router.open("QuickNotes")
        note_list = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert note_list.title == "QUICKNOTES"
        assert "Ask Adaeze" in note_list.items[0]
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="QUICKNOTES",
            selected_index=0,
        ))
        await asyncio.sleep(0)
        note_detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert note_detail.title == "QUICKNOTES · DETAIL"
        assert note_detail.items == ["Ask Adaeze about the design review"]

        await router.open("Activity")
        action_list = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert action_list.title == "ACTIVITY"
        assert "Calendar create" in action_list.items[0]
        assert "Drafted" in action_list.items[0]
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="ACTIVITY",
            selected_index=0,
        ))
        await asyncio.sleep(0)
        # The row already said label + state; the detail is a band over the
        # list, not a page repeating it.
        assert [m.title for m in transport.sent if isinstance(m, HudList)][-1] == "ACTIVITY"
        detail = [m for m in transport.sent if isinstance(m, AssistantOverlay)][0]
        assert detail.text is not None
        assert detail.text.startswith("Calendar create · Drafted · ref ")
        store.close()

    async def test_quicknotes_unifies_recordings_and_text_notes(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = DurableStore(tmp_path / "durable.sqlite3")
        notes = NoteStore(store)
        notes.create(
            note_id="note-1",
            idempotency_key="voice-1",
            text="Ask Adaeze about the design review",
            source=NoteSource(kind="voice.command"),
            now_us=10,
        )
        voice_notes = VoiceNoteStore(store)
        _retained_voice_note(voice_notes, "capture-1", base_us=20)
        voice_notes.replace_insights(
            "capture-1",
            engine="mlx-local",
            summary="The team agreed to ship Friday.",
            key_points=("Ship Friday", "QA Thursday"),
            todos=("Email release notes",),
            now_us=99,
        )
        router, transport, _ = make_router(
            tmp_path, notes=notes, voice_notes=voice_notes,
        )

        await router.open("QuickNotes")
        note_list = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert note_list.title == "QUICKNOTES"
        # Recordings first (marked with the ● record glyph), then exact
        # dictated text notes.
        assert "● Standup notes" in note_list.items[0]
        assert "Ask Adaeze" in note_list.items[1]
        assert note_list.selectable is True

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="QUICKNOTES",
            selected_index=0,
        ))
        await asyncio.sleep(0)
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "QUICKNOTES · NOTE"
        assert detail.items[0] == "Standup notes"
        assert "SUMMARY" in detail.items
        assert any("ship Friday" in item for item in detail.items)
        assert "KEY POINTS" in detail.items
        assert "TODOS" in detail.items
        # The newest recording advertises the transcript Q&A grammar.
        assert detail.hint is not None and "ask the last note" in detail.hint

        # Text rows keep their exact-text detail behaviour.
        await router.open("QuickNotes")
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="QUICKNOTES",
            selected_index=1,
        ))
        await asyncio.sleep(0)
        text_detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert text_detail.title == "QUICKNOTES · DETAIL"
        assert text_detail.items == ["Ask Adaeze about the design review"]

    async def test_live_recording_owns_the_quicknotes_page_with_tap_controls(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = DurableStore(tmp_path / "durable.sqlite3")
        voice_notes = VoiceNoteStore(store)
        _retained_voice_note(voice_notes, "capture-0", base_us=5)
        capture = FakeVoiceCapture(state="recording", title="Client call")
        router, transport, _ = make_router(
            tmp_path, voice_notes=voice_notes, voice_capture=capture,
        )

        await router.open("QuickNotes")
        controls = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert controls.title == "QUICKNOTES · RECORDING"
        assert controls.items == [
            "Save and finish", "Pause recording", "Discard recording",
        ]
        assert controls.selectable is True
        assert controls.hint is not None and "Client call" in controls.hint

        # Tapping Save drives the typed control, then re-renders truth: the
        # capture is no longer active, so the unified list returns.
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="QUICKNOTES",
            selected_index=0,
        ))
        await asyncio.sleep(0)
        assert capture.calls == ["stop"]
        titles = [m.title for m in transport.sent if isinstance(m, HudList)]
        # Saving is a transient band over the live page, not a page of its
        # own; the unified list is the next full frame.
        assert titles[-1] == "QUICKNOTES"
        assert "QUICKNOTES · SAVING" not in titles
        assert any(
            isinstance(m, AssistantOverlay) and m.text == "Saving quicknote…"
            for m in transport.sent
        )
        after = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert "● Standup notes" in after.items[0]

    async def test_discard_needs_its_own_confirm_row(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        capture = FakeVoiceCapture(
            state="paused", title="Risky", discard_pending=True,
        )
        router, transport, _ = make_router(tmp_path, voice_capture=capture)

        await router.open("QuickNotes")
        controls = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert controls.title == "QUICKNOTES · DISCARD?"
        assert controls.items == ["Confirm discard", "Keep recording"]


class TestGestureReflexes:
    async def test_menu_closed_repaints_the_last_exchange(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, status = make_router(tmp_path)
        status.set(last_reply="It is 72 degrees.", last_transcript="what is the weather")
        router.on_input(InputEvent(container_name="menu", selected_name="closed"))
        await asyncio.sleep(0.05)
        overlay = [m for m in transport.sent if isinstance(m, AssistantOverlay)]
        ctx = [m for m in transport.sent
               if isinstance(m, HudText) and m.container_id is ContainerId.CONTEXT]
        assert [(m.text, m.operation, m.dismiss_after_ms) for m in overlay] == [
            ("It is 72 degrees.", "replace", None),
            ("", "settle", 6_000),
        ]
        assert [m.text for m in ctx] == ["you  what is the weather"]

    async def test_repeat_with_no_history_says_so(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)
        router.on_input(InputEvent(container_name="sysmenu", selected_index=1))
        await asyncio.sleep(0.05)
        overlay = [m for m in transport.sent if isinstance(m, AssistantOverlay)]
        assert [(m.text, m.operation, m.dismiss_after_ms) for m in overlay] == [
            ("Nothing to repeat yet.", "replace", None),
            ("", "settle", 6_000),
        ]


class TestCapabilityOpenController:
    """'JOAT, show my messages' — surfaces open from speech alone."""

    async def test_voice_open_paints_messages_under_a_voice_generation(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(
            tmp_path, rows=[("Adele", "Send the mockups?", time.time())],
        )
        opener = CapabilityOpenController(lambda: router)
        assert await opener.handle_command("show my messages") == "Opening Messages."
        note_list = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert note_list.title == "MESSAGES"
        assert note_list.request_id is not None
        assert note_list.request_id.startswith(VOICE_REQUEST_PREFIX)

    async def test_spoken_aliases_reach_their_surfaces(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)
        opener = CapabilityOpenController(lambda: router)
        assert await opener.handle_command("read my texts") == "Opening Messages."
        assert await opener.handle_command("open the calendar") == "Opening Planning."
        assert await opener.handle_command("Show notifications") == "Opening Notifications."
        assert await opener.handle_command("how is the weather") is None
        assert await opener.handle_command("open the pod bay doors") is None

    async def test_reflect_quicknotes_shows_the_live_recording_controls(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(
            tmp_path, voice_capture=FakeVoiceCapture(state="recording", title="Standup"),
        )
        opener = CapabilityOpenController(lambda: router)
        await opener.reflect_quicknotes()
        control_list = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert control_list.title == "QUICKNOTES · RECORDING"
        assert control_list.request_id is not None
        assert control_list.request_id.startswith(VOICE_REQUEST_PREFIX)


class TestMessageListRow:
    def test_unread_thread_carries_the_attention_marker(self) -> None:
        row = CapabilityRouter._message_list_row(MessageThread(
            "Adele", "handle-1", "Send the mockups?", time.time(), True, unread=True,
        ))
        assert " ● Adele" in row
        assert "(unread)" not in row

    def test_read_thread_stays_unmarked(self) -> None:
        row = CapabilityRouter._message_list_row(MessageThread(
            "Adele", "handle-1", "Send the mockups?", time.time(), True,
        ))
        assert "●" not in row


class TestVoiceOpenChain:
    """The full Mac half of "JOAT, show my messages": real command router,
    real open controller, real capability router — one voice-generation list
    lands on the transport exactly as the plugin will adopt it."""

    async def test_punctuated_speech_opens_messages_end_to_end(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from joat.services.commands import VoiceCommandRouter
        from joat.services.models import ModelRegistry

        router, transport, _ = make_router(
            tmp_path, rows=[("Adele", "Send the mockups?", time.time())],
        )

        class _NoReplies:
            def is_compose_armed(self) -> bool:
                return False

            async def handle_command(self, text: str) -> str | None:
                del text
                return None

        commands = VoiceCommandRouter(
            _NoReplies(),  # type: ignore[arg-type]
            ModelRegistry([]),
            capability_opens=CapabilityOpenController(lambda: router),
        )

        # Whisper's punctuation and casing, post wake-strip.
        assert await commands("Show my messages.") == "Opening Messages."

        painted = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert painted.title == "MESSAGES"
        assert painted.request_id is not None
        assert painted.request_id.startswith(VOICE_REQUEST_PREFIX)
        assert any("Adele" in item for item in painted.items)


class TestReenteringALiveBrainstorm:
    """docs/SESSION_MODEL.md:46 — "open Brainstorm during an active run →
    restore the current conversation."

    The opposite shipped: any non-empty transcript painted state="settled",
    which renders DONE on the glass and makes the tap a no-op. Opening the
    menu mid-session told the wearer their live conversation had finished
    and left no way back into it.
    """

    async def test_opening_the_row_mid_session_restores_it(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        replayed: list[bool] = []

        async def replay() -> bool:
            replayed.append(True)
            return True                      # a session IS live

        router, transport, _ = make_router(tmp_path, brainstorm_replay=replay)
        await router.open("Brainstorm")

        assert replayed == [True]
        # The live sink owns the frame; the router must not paint a second,
        # reconstructed one over it — that is how the streaming turn in
        # progress got replaced by a "settled" recap.
        assert not [m for m in transport.sent if isinstance(m, BrainstormHudView)]

    async def test_an_idle_row_still_opens_normally(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        async def replay() -> bool:
            return False                     # nothing live

        router, transport, _ = make_router(tmp_path, brainstorm_replay=replay)
        await router.open("Brainstorm")

        frames = [m for m in transport.sent if isinstance(m, BrainstormHudView)]
        assert frames and frames[-1].state == "ready"


class TestTheSessionPicker:
    """docs/SESSION_MODEL.md:47 — "open Brainstorm while idle → show recent
    sessions plus NEW."

    It was four lines of dead text inside an activity string. The wearer
    could SEE that past sessions existed and could not open one, because
    nothing carried an id to select them by.
    """

    class Past:
        def __init__(self, sid, topic, turns):
            self.session_id = sid
            self.topic = topic
            self.turns = turns

        @property
        def title(self):
            return self.topic

        def age(self):
            return "2m ago"

    def _rows(self):
        return [
            self.Past("aaa", "cold-start latency", (("CODEX", "x"),)),
            self.Past("bbb", "tabs or spaces", (("CLAUDE", "y"), ("JOAT", "z"))),
        ]

    async def test_the_idle_row_lists_real_sessions_with_new_on_top(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        rows = self._rows()
        router, transport, _ = make_router(
            tmp_path, brainstorm_sessions=lambda: rows)
        await router.open("Brainstorm")

        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists, "the picker must be a real selectable list, not prose"
        items = lists[-1].to_wire()["items"]
        assert items[0] == "+ New brainstorm"
        assert "cold-start latency" in items[1]
        assert "2 turns" in items[2]
        assert lists[-1].to_wire()["selectable"] is True

    async def test_the_picker_survives_a_session_closing_in_process(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        """The audit's BSI-03: the picker was gated on the in-process recap
        having ZERO rows, so the moment any session closed in this process
        the idle view became that session's dead text and the picker was
        unreachable until a restart. Idle means picker, unconditionally —
        docs/SESSION_MODEL.md:47 has no other clause."""
        rows = self._rows()

        class Recap:
            topic = "tabs or spaces"
            speakers = ("codex", "claude")

            def rows(self, *a: object, **k: object) -> list[str]:
                return ["CLAUDE — y", "JOAT — z"]   # a session HAS closed

        router, transport, _ = make_router(
            tmp_path, brainstorm_sessions=lambda: rows,
            brainstorm_recap=lambda: Recap())
        await router.open("Brainstorm")

        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists, "the recap text hid the picker"
        items = lists[-1].to_wire()["items"]
        assert items[0] == "+ New brainstorm"

    async def test_tapping_a_row_opens_that_session(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        rows = self._rows()
        opened: list[str] = []

        async def continue_it(session_id: str) -> bool:
            opened.append(session_id)
            return True

        router, _t, _s = make_router(
            tmp_path, brainstorm_sessions=lambda: rows,
            brainstorm_continue=continue_it)
        await router.open("Brainstorm")
        await router._select_brainstorm(2)      # row 0 is NEW

        assert opened == ["bbb"], "the id, not the row number"

    async def test_a_row_that_moved_is_not_opened_by_position(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        """A session can finish between the paint and the tap. Indexing the
        store again would open whatever had shifted into that slot."""
        rows = self._rows()
        opened: list[str] = []

        async def continue_it(session_id: str) -> bool:
            opened.append(session_id)
            return True

        router, _t, _s = make_router(
            tmp_path, brainstorm_sessions=lambda: rows,
            brainstorm_continue=continue_it)
        await router.open("Brainstorm")
        rows.insert(0, self.Past("ccc", "something new", (("CODEX", "q"),)))

        await router._select_brainstorm(2)
        assert opened == ["bbb"], "resolved against what was DRAWN"


class TestTheTerminalShowsItsSessions:
    """The idle terminal said: Say "terminal sessions" to choose a work
    session. The wearer's response: "why do I have to say something to
    choose a work session? WHAT DOES THAT EVEN MEAN?"

    A chooser that requires you to already know the words is not a chooser.
    """

    async def test_opening_it_lists_the_sessions(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        rows = [("work", "▸ work · idle"),
                ("calendar", "  calendar · working · 2 waiting")]
        router, transport, _ = make_router(tmp_path, terminal_rows=lambda: rows)
        await router.open("Agent Terminal")

        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists, "the sessions must be rows you can tap, not a phrase"
        items = lists[-1].to_wire()["items"]
        assert "work" in items[0] and "calendar" in items[1]
        assert lists[-1].to_wire()["selectable"] is True

    async def test_tapping_a_row_focuses_that_session(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        focused: list[str] = []

        async def focus(name: str) -> None:
            focused.append(name)

        rows = [("work", "▸ work · idle"), ("calendar", "  calendar · idle")]
        router, _t, _s = make_router(
            tmp_path, terminal_rows=lambda: rows, terminal_focus=focus)
        await router.open("Agent Terminal")
        await router._select_terminal(1)
        assert focused == ["calendar"]

    async def test_live_output_is_never_hidden_behind_a_picker(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        """The wearer opened the terminal to WATCH work. A chooser drawn
        over a running session hides the thing they came for."""
        painted: list[str] = []

        async def view() -> None:
            painted.append("live")

        # The roster is ASKED, not parsed. This test used to pass
        # "▸ work · running" and match on the word — but the state word the
        # roster emits is "working", so the guard never fired and the
        # picker would have drawn straight over live output. A fixture that
        # invents a string production never sends proves nothing.
        rows = [("work", "▸ work · working · run the audit")]
        router, transport, _ = make_router(
            tmp_path, terminal_rows=lambda: rows, terminal_view=view,
            terminal_busy=lambda: True)
        await router.open("Agent Terminal")

        assert painted == ["live"]
        assert not [m for m in transport.sent if isinstance(m, HudList)]


class TestARowTapCrossesTheRealGuards:
    """The picker tests call _select_* directly, which skips the request-id
    and capability checks a genuine tap goes through. Those guards drop
    stale selections — and would drop a real one just as quietly, with the
    wearer seeing a tap that did nothing.
    """

    class Past:
        def __init__(self, sid, topic, turns):
            self.session_id, self.topic, self.turns = sid, topic, turns

        @property
        def title(self):
            return self.topic

        def age(self):
            return "2m ago"

    async def test_a_brainstorm_row_tap_opens_that_session(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        opened: list[str] = []

        async def continue_it(session_id: str) -> bool:
            opened.append(session_id)
            return True

        rows = [self.Past("aaa", "cold-start latency", (("CODEX", "x"),)),
                self.Past("bbb", "tabs or spaces", (("CLAUDE", "y"),))]
        router, transport, _ = make_router(
            tmp_path, brainstorm_sessions=lambda: rows,
            brainstorm_continue=continue_it)
        await router.open("Brainstorm")

        lists = [m for m in transport.sent if isinstance(m, HudList)]
        router.on_input(InputEvent(
            container_name="capdata", selected_index=2,
            selected_name="BRAINSTORM",
            request_id=getattr(lists[-1], "request_id", None), trace_id="hud",
        ))
        for _ in range(40):
            await asyncio.sleep(0.01)
            if opened:
                break
        assert opened == ["bbb"], "row 0 is NEW, so index 2 is the second session"

    async def test_a_stale_tap_is_dropped(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        opened: list[str] = []

        async def continue_it(session_id: str) -> bool:
            opened.append(session_id)
            return True

        rows = [self.Past("aaa", "t", (("CODEX", "x"),))]
        router, _t, _s = make_router(
            tmp_path, brainstorm_sessions=lambda: rows,
            brainstorm_continue=continue_it)
        await router.open("Brainstorm")
        router.on_input(InputEvent(
            container_name="capdata", selected_index=1,
            selected_name="BRAINSTORM", request_id="a-request-that-moved-on",
            trace_id="hud",
        ))
        await asyncio.sleep(0.05)
        assert opened == []

    async def test_a_terminal_row_tap_focuses_that_lane(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        focused: list[str] = []

        async def focus(name: str) -> None:
            focused.append(name)

        router, transport, _ = make_router(
            tmp_path,
            terminal_rows=lambda: [("work", "▸ work · idle"),
                                   ("calendar", "  calendar · idle")],
            terminal_focus=focus)
        await router.open("Agent Terminal")

        lists = [m for m in transport.sent if isinstance(m, HudList)]
        router.on_input(InputEvent(
            container_name="capdata", selected_index=1,
            selected_name="AGENT TERMINAL",
            request_id=getattr(lists[-1], "request_id", None), trace_id="hud",
        ))
        for _ in range(40):
            await asyncio.sleep(0.01)
            if focused:
                break
        assert focused == ["calendar"]


class TestTheBusySignalComesFromTheRoster:
    """Not from parsing rendered row text.

    The picker guard matched " · running" against the drawn rows. The word
    AgentTerminalRoster emits is "working", so the guard never fired once —
    it would have drawn the chooser over a lane that was mid-run, hiding
    the work the wearer opened the terminal to watch. The test that claimed
    to prevent it supplied a row string production never sends.
    """

    async def test_the_real_roster_reports_busy(self) -> None:
        from joat.services.agent_terminal import AgentTerminalRoster

        class Slow:
            def __init__(self, gate): self._gate = gate
            async def stream(self, prompt):
                await self._gate.wait()
                yield "done"

        gate = asyncio.Event()
        roster = AgentTerminalRoster([("work", Slow(gate))])
        assert roster.busy is False

        await roster.send("run the audit")
        assert roster.busy is True, (
            "a lane mid-run must report busy, whatever word its row happens "
            "to render"
        )
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert roster.busy is False


class TestATapThatFailsSaysSo:
    """Gridding the tap-to-select paths the way the spoken verbs were
    gridded, with the same result: three silences.

    The dispatch had a `try` with only a `finally`, so a handler that
    raised — a lane that vanished between the paint and the tap, a store
    that could not be read — became an unhandled task exception. The tap
    did nothing, said nothing, and the traceback went where the wearer
    will never look. And a row that had scrolled off the list returned
    silently, which reads as "the glasses ignored me" rather than "the
    list moved".
    """

    async def test_a_raising_handler_is_caught_and_reported(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        async def focus(name: str) -> None:
            raise RuntimeError("lane vanished")

        router, transport, _status = make_router(tmp_path, terminal_focus=focus)
        router._terminal_row_names = ["work"]
        router._active_request_id = "req"
        router._active_capability = router._canonical_capability("AGENT TERMINAL")

        # (name, index, trace_id, request_id) — the production order.
        await router._select_capability_row("AGENT TERMINAL", 0, "trace", "req")
        await asyncio.sleep(0.02)

        spoken = [
            m.text for m in transport.sent
            if isinstance(m, AssistantOverlay) and m.text
        ]
        assert spoken, "the tap raised into nothing"

    async def test_a_terminal_row_that_scrolled_away_says_the_list_moved(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        async def focus(name: str) -> None:
            return None

        router, transport, _status = make_router(tmp_path, terminal_focus=focus)
        await router._select_terminal(3)          # nothing was drawn
        await asyncio.sleep(0.02)

        spoken = [
            m.text for m in transport.sent
            if isinstance(m, AssistantOverlay) and m.text
        ]
        assert spoken and "no longer listed" in spoken[-1]

    async def test_a_brainstorm_row_that_scrolled_away_says_so_too(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        async def continue_it(session_id: str) -> bool:
            return True

        router, transport, _status = make_router(
            tmp_path, brainstorm_continue=continue_it,
        )
        await router._select_brainstorm(5)
        await asyncio.sleep(0.02)

        spoken = [
            m.text for m in transport.sent
            if isinstance(m, AssistantOverlay) and m.text
        ]
        assert spoken and "no longer listed" in spoken[-1]
