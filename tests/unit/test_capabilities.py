"""CapabilityRouter: the on-glasses menu's data views.

The wearer's hardware report that motivated this: double-tap opened 'a menu
modal, but not the one we designed'. The plugin now shows the designed rows;
this router is the half that draws each view.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from joat.channels.cards import Card, CardLog, Urgency
from joat.channels.imessage import MessageEntry, MessageHistoryPage, MessageThread
from joat.core.protocol import (
    AudioControl,
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
from joat.services.message_reply import MessageReplyController
from joat.services.voice_note_capture import LiveLine, VoiceNoteCaptureSnapshot
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


class HeldHistoryMessages(FakeMessages):
    """Keep one synthetic thread read in flight across a navigation change."""

    def __init__(
        self, *, hold_older: bool = False, error: OSError | None = None,
    ) -> None:
        super().__init__([
            ("Thread A", "First thread", 1.0),
            ("Thread B", "Second thread", 2.0),
        ])
        self.hold_older = hold_older
        self.error = error
        self.started = threading.Event()
        self.release = threading.Event()

    def thread_message_page(
        self,
        thread: MessageThread,
        *,
        limit: int = 8,
        before_row_id: int | None = None,
    ) -> MessageHistoryPage:
        del limit
        older = before_row_id is not None
        if thread.sender == "Thread A" and older == self.hold_older:
            self.started.set()
            if not self.release.wait(timeout=5):
                raise AssertionError("held Messages read was not released")
            if self.error is not None:
                raise self.error
        return MessageHistoryPage(
            entries=(MessageEntry(thread.text, thread.at, False, row_id=9 if older else 10),),
            has_older=not older,
        )


class OlderHistoryMessages(FakeMessages):
    """A full first thread page with older history waiting in chat.db."""

    def __init__(self) -> None:
        super().__init__([("Dele", "Newest 30", time.time())])
        self.page_calls: list[int | None] = []

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
            # Ten one-row messages: exactly one ten-row reader page.
            return MessageHistoryPage(entries=tuple(
                MessageEntry(f"Newest {row}", float(row), row % 2 == 0, row_id=row)
                for row in range(30, 20, -1)
            ), has_older=True)
        assert before_row_id == 21
        return MessageHistoryPage(entries=tuple(
            MessageEntry(f"Older {row}", float(row), False, row_id=row)
            for row in range(20, 17, -1)
        ), has_older=False)


class SentReplyMessages(FakeMessages):
    """A two-page thread; chat.db records the wearer's reply only later."""

    def __init__(self, *, records_after: int | None) -> None:
        super().__init__([("Dele", "Preview", time.time())])
        #: The read count from which chat.db has the sent row; None = never.
        self.records_after = records_after
        self.reads = 0

    def thread_message_page(
        self,
        thread: MessageThread,
        *,
        limit: int = 8,
        before_row_id: int | None = None,
    ) -> MessageHistoryPage:
        del thread, limit, before_row_id
        self.reads += 1
        entries = (MessageEntry("word " * 140 + "END", 5.0, False, row_id=5),)
        if self.records_after is not None and self.reads >= self.records_after:
            entries = (MessageEntry("On my way!", 6.0, True, row_id=6, delivery="sent"), *entries)
        return MessageHistoryPage(entries=entries, has_older=False)


def conversation_frames(transport: FakeTransport) -> list[HudList]:
    return [
        m for m in transport.sent
        if isinstance(m, HudList) and m.title == "MESSAGES · CONVERSATION"
    ]


def page_event(event_type: int, trace_id: str, request_id: str | None) -> InputEvent:
    return InputEvent(
        container_name="cappage", selected_name="MESSAGES", event_type=event_type,
        trace_id=trace_id, request_id=request_id,
    )


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


async def wait_until(predicate, *, timeout_s: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    """Poll until ``predicate`` holds, bounded by wall-clock time.

    The router hands SQLite reads to worker threads (``asyncio.to_thread``),
    so a fixed count of ``sleep(0)`` loop turns can be exhausted in
    microseconds before the thread has returned. A deadline is the only
    bound that survives scheduler jitter; the fast path still returns on
    the first turn that satisfies the predicate.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.001)


async def collect_note_reader(router, transport, *, request_id, trace_id):  # type: ignore[no-untyped-def]
    """Read actual serialized pages, replaying each page gesture once."""
    pages = []
    for expected_index in range(100):
        frame = [m for m in transport.sent if isinstance(m, HudList)][-1]
        wire = frame.to_wire()
        assert wire["items"] == frame.items, "reader data was truncated on the wire"
        assert frame.request_id == request_id
        assert frame.selectable is False
        assert 1 < len(frame.items) <= 11
        assert all(sum(
            8 if char == "\t" else 2
            if ord(char) > 0xFFFF or unicodedata.east_asian_width(char) in "WF" else 1
            for char in row.rstrip("\r\n")
        ) <= 62 for row in frame.items[1:])
        assert all(len(row.encode("utf-16-le")) // 2 <= 64 for row in wire["items"])
        assert all(len(row.rstrip("\r\n")) <= 31 for row in frame.items[1:]
                   if row.rstrip("\r\n") and set(row.rstrip("\r\n")) == {"漢"})
        counter = re.match(r"^(\d+)/(\d+) · ", frame.items[0])
        assert counter is not None, "reader must identify its actual page"
        index, total = int(counter[1]) - 1, int(counter[2])
        assert index == expected_index and total > index
        hint = wire.get("hint", "")
        assert ("up previous" in hint) == (index > 0)
        assert ("down more" in hint) == (index + 1 < total)
        assert "left x2: back" in hint
        pages.append(wire)
        if index + 1 == total:
            return pages
        before = len(transport.sent)
        event = InputEvent(
            container_name="cappage", selected_name="QUICKNOTES",
            selected_index=index, event_type=EventType.SCROLL_BOTTOM,
            request_id=request_id, trace_id=trace_id,
        )
        router.on_input(event)
        router.on_input(event)
        await wait_until(lambda: len(transport.sent) > before)
        await asyncio.sleep(0)
        assert len(transport.sent) == before + 1, "duplicate gesture skipped a page"
        router.on_input(event)  # A delayed duplicate still names the old page.
        await asyncio.sleep(0)
        assert len(transport.sent) == before + 1, "delayed duplicate skipped a page"
    raise AssertionError("reader did not finish")


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
            mode=self._mode,
            holding=self._holding,
            recorded_s=self.recorded_s,
        )

    # FEAT-002 live-note surface.
    _mode = "continuous"
    _holding = False
    recorded_s = 0
    lines: tuple[LiveLine, ...] = ()
    silent_s = 0.0

    def live_lines(self, limit: int = 12) -> tuple[LiveLine, ...]:
        return self.lines[-limit:]

    def silent_for_s(self) -> float:
        return self.silent_s

    def add_listener(self, listener) -> None:  # type: ignore[no-untyped-def]
        self.listener = listener

    def control_toggle(self) -> str:
        self.calls.append("toggle")
        self._state = "paused" if self._state == "recording" else "recording"
        return "Toggled Voice Note."

    def control_hold_start(self) -> str | None:
        self.calls.append("hold_start")
        if self._state != "paused":
            return None
        self._state, self._holding = "recording", True
        return "Listening."

    def control_hold_end(self) -> str | None:
        self.calls.append("hold_end")
        if not self._holding:
            return None
        self._state, self._holding = "paused", False
        return "Paused."

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

    def control_start(self, mode: str = "continuous") -> str:
        self.calls.append(f"start:{mode}")
        self._active = True
        self._mode = mode
        self._state = "recording" if mode == "continuous" else "paused"
        return "Recording Voice Note."


class FakeMessageUI:
    """Duck-typed MessageUIController: the conversation page's reply control."""

    def __init__(self, *, refusal: str = "") -> None:
        #: Reply-flow calls only; `retired` counts the visit resets that
        #: opening or leaving Messages performs regardless of the page.
        self.calls: list[str] = []
        self.selections: list[tuple[str | None, str]] = []
        self.retired = 0
        #: What begin_reply answers: "" when the question went up, else why not.
        self.refusal = refusal

    def matches(self, request_id: str | None, selection_id: str | None) -> bool:
        return False

    @property
    def has_visit(self) -> bool:
        return False

    def select(self, request_id: str | None, selection_id: str, current) -> None:  # type: ignore[no-untyped-def]
        self.selections.append((request_id, selection_id))

    def conversation_ready(self) -> None:
        self.calls.append("conversation_ready")

    def retire(self) -> None:
        self.retired += 1

    def cancel(self, action_id: str) -> None:
        self.calls.append(f"cancel:{action_id}")

    async def begin_new(self) -> str:
        self.calls.append("begin_new")
        return "new"

    async def begin_reply(self) -> str:
        self.calls.append("begin_reply")
        return self.refusal


def receipts(transport: FakeTransport) -> list[str]:
    """Every receipt band the router put over the page, in order."""
    return [
        m.text for m in transport.sent
        if isinstance(m, AssistantOverlay) and m.presentation == "receipt"
    ]


NOTE_CONTROL_ROWS = ["● Record", "✎ Quick note · hold to talk"]


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
    brainstorm_recap=None,
    brainstorm_replay=None,
    brainstorm_sessions=None,
    brainstorm_continue=None,
    brainstorm_unavailable=None,
    terminal_rows=None,
    terminal_focus=None,
    terminal_view=None,
    terminal_busy=None,
    terminal_open=None,
    message_ui=None,
    ask=None,
    navigate=None,
    places=None,
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
        brainstorm_recap=brainstorm_recap,
        brainstorm_replay=brainstorm_replay,
        brainstorm_sessions=brainstorm_sessions,
        brainstorm_continue=brainstorm_continue,
        brainstorm_unavailable=brainstorm_unavailable,
        terminal_rows=terminal_rows,
        terminal_focus=terminal_focus,
        terminal_view=terminal_view,
        terminal_busy=terminal_busy,
        terminal_open=terminal_open,
        message_ui=message_ui,
        ask=ask,
        navigate=navigate,
        places=places,
    )
    return router, transport, status


class TestCapabilityViews:
    async def test_note_list_pages_through_tappable_rows(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The firmware list reports taps, never scrolls: each archive page
        names its neighbours as rows and says which rows they are."""
        with DurableStore(tmp_path / "paged-notes.sqlite3") as durable:
            notes = NoteStore(durable)
            for index in range(1, 24):
                notes.create(
                    note_id=f"note-{index:02d}", idempotency_key=f"key-{index:02d}",
                    text=f"Fixture note {index}", source=NoteSource(kind="dictation"),
                    now_us=1_000_000 * index,
                )
            router, transport, _ = make_router(tmp_path, notes=notes)
            await router.open("QuickNotes")
            first = [m for m in transport.sent if isinstance(m, HudList)][-1]
            assert first.title == "QUICKNOTES"
            # Every page is a control surface: record and quick note lead,
            # then fifteen notes, then the paging row — twenty rows at most.
            assert first.items[:2] == NOTE_CONTROL_ROWS
            assert first.items[2].endswith("Fixture note 23")
            assert first.items[16].endswith("Fixture note 9")
            assert first.items[-1] == "▼ Older notes"
            assert len(first.items) == 18
            assert first.selectable is True
            assert first.page_controls == (None, 17)
            assert first.quick_notes_page == (0, 2)

            router.on_input(InputEvent(
                container_name="cappage", selected_name="QUICKNOTES · LIST", selected_index=0,
                event_type=EventType.SCROLL_BOTTOM,
            ))
            await wait_until(lambda: router._note_list is not None and router._note_list.index == 1)
            await wait_until(lambda: any(
                isinstance(m, HudList) and m.title == "QUICKNOTES" and m.items[-1] == "▲ Newer notes"
                for m in transport.sent
            ))
            older = [m for m in transport.sent if isinstance(m, HudList)][-1]
            assert older.items[:2] == NOTE_CONTROL_ROWS
            assert older.items[2].endswith("Fixture note 8")
            assert older.items[9].endswith("Fixture note 1")
            assert len(older.items) == 11
            assert older.page_controls == (10, None)
            assert older.quick_notes_page == (1, 2)

            # A tap on the paging row never opens a note: it is past the rows.
            router.on_input(InputEvent(
                container_name="capdata", selected_name="QUICKNOTES", selected_index=10,
                quick_notes_page_index=1,
            ))
            await asyncio.sleep(0)
            assert router._note_reader is None

    async def test_brainstorm_row_shows_the_missing_seat_instead_of_a_promise(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A missing CLI seat used to surface only after the tap: the row opened
        on "Ready for a question" and the tap went nowhere useful."""
        gone = BrainstormHudView(
            state="unavailable", topic="Brainstorm", collaborators=["Claude"],
            activity="codex CLI missing on the Mac — install to enable.",
        )
        router, transport, _ = make_router(
            tmp_path, brainstorm_unavailable=lambda: gone,
            brainstorm_sessions=lambda: [],
        )
        await router.open("Brainstorm")
        frames = [item for item in transport.sent if isinstance(item, BrainstormHudView)]
        assert frames[-1].state == "unavailable"
        assert "codex CLI missing" in frames[-1].activity

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
        # The idle frame invites a tap; voice works from anywhere and the
        # frame no longer tells the wearer what to say.
        assert frames[0].state == "ready"
        assert frames[0].activity == "Tap to speak a topic."

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
            "➤ Navigate to a place",
            "I’ll confirm the place. Then choose Walk or Cycle.",
        ]
        assert lists[0].hint == "tap: choose · left x2: back"
        assert lists[0].selectable is True
        assert lists[0].request_id == "navigation-entry"
        assert not any("JOAT" in item for item in lists[0].items)

    async def test_navigation_page_lists_saved_places_first_and_a_tap_starts_their_route(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        from joat.core.protocol import SavedPlace

        started: list[str] = []

        async def navigate(destination: str) -> str | None:
            started.append(destination)
            return None

        saved = [
            SavedPlace(place_id="p1", name="Home", address="12 Oak St"),
            SavedPlace(place_id="p2", name="Work", latitude=35.2, longitude=-97.4),
        ]
        router, transport, _ = make_router(tmp_path, navigate=navigate, places=lambda: saved)

        await router.open("Navigation", request_id="navigation-places")
        page = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert page.items == [
            "➤ Home",
            "➤ Work",
            "➤ Navigate to a place",
            "Add places on the phone page.",
        ]
        assert page.selectable is True

        router.on_input(InputEvent(
            container_name="capdata", selected_name="NAVIGATION", selected_index=1,
            request_id="navigation-places",
        ))
        await wait_until(lambda: started == ["Work"])
        # The open question still sits after the places.
        router.on_input(InputEvent(
            container_name="capdata", selected_name="NAVIGATION", selected_index=3,
            request_id="navigation-places",
        ))
        await asyncio.sleep(0.01)
        assert started == ["Work"]

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
            "➤ Navigate to a place",
            "I’ll confirm the place. Then choose Walk or Cycle.",
        ]
        assert frame.selectable is True
        assert frame.hint == "tap: choose · left x2: back"
        assert frame.request_id == "navigation-menu-entry"

    async def test_navigation_row_asks_where_to_then_starts_the_route(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """The tapped control row asks on the lens; the answer is the route."""
        asked: list[tuple[str, str]] = []
        routed: list[str] = []

        async def ask(question: str, field: str, consume) -> bool:  # type: ignore[no-untyped-def]
            asked.append((question, field))
            await consume("the library")
            return True

        async def navigate(destination: str) -> str | None:
            routed.append(destination)
            return "Heading to The Library — Walk or Cycle?"

        router, transport, _ = make_router(tmp_path, ask=ask, navigate=navigate)
        await router.open("Navigation", request_id="navigation-tap")
        router.on_input(InputEvent(
            container_name="capdata", selected_name="NAVIGATION",
            selected_index=0, request_id="navigation-tap",
        ))
        await wait_until(lambda: bool(routed))
        assert asked == [("Where to?", "destination")]
        assert routed == ["the library"]
        assert receipts(transport) == ["Heading to The Library — Walk or Cycle?"]

        # The second row is copy, not a control: it asks nothing.
        router.on_input(InputEvent(
            container_name="capdata", selected_name="NAVIGATION",
            selected_index=1, request_id="navigation-tap",
        ))
        await asyncio.sleep(0.01)
        assert asked == [("Where to?", "destination")]
        assert routed == ["the library"]

    async def test_navigation_row_without_a_route_starter_says_so(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)
        await router.open("Navigation", request_id="navigation-inert")
        router.on_input(InputEvent(
            container_name="capdata", selected_name="NAVIGATION",
            selected_index=0, request_id="navigation-inert",
        ))
        await wait_until(lambda: bool(receipts(transport)))
        assert receipts(transport) == ["Navigation cannot be started from this Mac."]

    async def test_navigation_row_reports_a_question_that_never_reached_the_lens(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        routed: list[str] = []

        async def ask(question: str, field: str, consume) -> bool:  # type: ignore[no-untyped-def]
            return False

        async def navigate(destination: str) -> str | None:
            routed.append(destination)
            return None

        router, transport, _ = make_router(tmp_path, ask=ask, navigate=navigate)
        await router.open("Navigation", request_id="navigation-unpublished")
        await router._select_capability_row("NAVIGATION", 0, "hud", "navigation-unpublished")
        assert routed == []
        assert receipts(transport) == ["The question did not reach the glasses — try again."]

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
        # The page carries the conversation only: no spoken-grammar rows,
        # and no tap-to-reply either, since no message UI can take the tap.
        assert detail.items == ["1/1 · Adaeze", "Adaeze: Call me"]
        assert detail.hint == "read-only · left x2: back"
        assert detail.selectable is False
        assert detail.page_controls is None
        assert detail.reader_page == ("hud-12", 0, 1)

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
        await wait_until(lambda: bool(conversation_frames(transport)))

        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "MESSAGES · CONVERSATION"
        assert detail.request_id == "messages-context"
        # Newest first, one whole message per logical line.
        assert detail.items == [
            "1/1 · Dele",
            "Dele: Great\n",
            "You: On my way\n",
            "Dele: Are you coming?",
        ]
        assert detail.hint == "read-only · left x2: back"
        assert detail.reader_page == ("hud-context", 0, 1)

    @pytest.mark.parametrize("index", [0, 1, 3, 4, 57])
    async def test_a_tap_anywhere_on_the_thread_begins_the_prepared_reply(
        self, tmp_path, index: int,
    ) -> None:  # type: ignore[no-untyped-def]
        """A replyable thread with a message UI is one tappable reader; any
        tap on it, whatever row the Hub reports, starts the prepared reply
        the spoken grammar would."""
        replies = FakeReplies()
        message_ui = FakeMessageUI()
        router, transport, _ = make_router(tmp_path, replies=replies, message_ui=message_ui)
        router._messages = HistoryMessages([  # type: ignore[assignment]
            ("Dele", "Great", time.time()),
        ])
        await router.open("Messages", request_id="messages-reply")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Dele ·"),
            trace_id="hud-reply",
            request_id="messages-reply",
        ))
        await wait_until(lambda: bool(conversation_frames(transport)))
        assert message_ui.selections == [("messages-reply", "hud-reply")]
        assert message_ui.retired == 1  # opening Messages resets any old visit
        assert message_ui.calls == ["conversation_ready"]
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.items == [
            "1/1 · Dele",
            "Dele: Great\n",
            "You: On my way\n",
            "Dele: Are you coming?",
        ]
        assert detail.hint == "tap: reply · left x2: back"
        assert detail.selectable is True
        assert detail.page_controls is None
        assert not any("Reply" in row for row in detail.items)

        # A stale request cannot reply.
        router.on_input(InputEvent(
            container_name="capmessage", selected_index=index,
            trace_id="hud-reply", request_id="messages-stale",
        ))
        await asyncio.sleep(0.01)
        assert message_ui.calls == ["conversation_ready"]

        before = len(transport.sent)
        router.on_input(InputEvent(
            container_name="capmessage", selected_index=index,
            trace_id="hud-reply", request_id="messages-reply",
        ))
        await wait_until(lambda: "begin_reply" in message_ui.calls)
        assert message_ui.calls == ["conversation_ready", "begin_reply"]
        assert router._message_selection_active is True
        # The tap never re-targets: the Mac-side selection is unchanged and
        # an accepted question paints nothing over it.
        assert len(replies.selected) == 1
        await asyncio.sleep(0.01)
        assert receipts(transport) == []
        assert len(transport.sent) == before

    async def test_a_refused_thread_tap_says_why_on_the_glass(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        message_ui = FakeMessageUI(refusal="Finish or cancel the current message first.")
        router, transport, _ = make_router(
            tmp_path, rows=[("Dele", "Great", time.time())], replies=FakeReplies(),
            message_ui=message_ui,
        )
        await router.open("Messages", request_id="messages-refused")
        await router._select_capability_row("MESSAGES", 1, "hud-refused", "messages-refused")
        router.on_input(InputEvent(
            container_name="capmessage", selected_index=2,
            trace_id="hud-refused", request_id="messages-refused",
        ))
        await wait_until(lambda: bool(receipts(transport)))
        assert receipts(transport) == ["Finish or cancel the current message first."]

    async def test_a_thread_tap_without_a_message_ui_says_replying_is_unavailable(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(
            tmp_path, rows=[("Dele", "Great", time.time())], replies=FakeReplies(),
        )
        await router.open("Messages", request_id="messages-no-ui")
        await router._select_capability_row("MESSAGES", 1, "hud-no-ui", "messages-no-ui")
        router.on_input(InputEvent(
            container_name="capmessage", selected_index=1,
            trace_id="hud-no-ui", request_id="messages-no-ui",
        ))
        await wait_until(lambda: bool(receipts(transport)))
        assert receipts(transport) == ["Replying is unavailable in this session."]

    async def test_a_thread_tap_before_any_conversation_is_open_is_dropped(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        message_ui = FakeMessageUI()
        router, transport, _ = make_router(
            tmp_path, rows=[("Dele", "Great", time.time())], replies=FakeReplies(),
            message_ui=message_ui,
        )
        await router.open("Messages", request_id="messages-picker")
        before = len(transport.sent)
        router.on_input(InputEvent(
            container_name="capmessage", selected_index=1,
            trace_id="forged", request_id="messages-picker",
        ))
        await asyncio.sleep(0.01)
        assert "begin_reply" not in message_ui.calls
        assert len(transport.sent) == before

    async def test_a_read_only_thread_is_not_tappable_even_with_a_message_ui(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        message_ui = FakeMessageUI()
        router, transport, _ = make_router(
            tmp_path, rows=[("Dele", "Great", time.time())], message_ui=message_ui,
        )
        await router.open("Messages", request_id="messages-read-only")
        router.on_input(InputEvent(
            container_name="capdata", selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Dele ·"),
            trace_id="hud-read-only", request_id="messages-read-only",
        ))
        await wait_until(lambda: bool(conversation_frames(transport)))
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.items == ["1/1 · Dele", "Dele: Great"]
        assert detail.hint == "read-only · left x2: back"
        assert detail.selectable is False
        assert "conversation_ready" not in message_ui.calls
        assert router._message_selection_active is False

    async def test_a_long_message_reads_whole_across_thread_pages(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, replies=replies)
        messages = LongHistoryMessages([("Dele", "Preview", time.time())])
        router._messages = messages  # type: ignore[assignment]
        await router.open("Messages", request_id="messages-full-reader")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Dele ·"),
            trace_id="hud-reader-conversation",
            request_id="messages-full-reader",
        ))
        await wait_until(lambda: bool(conversation_frames(transport)))
        first_page = conversation_frames(transport)[-1]
        assert first_page.items[0] == "1/2 · Dele"
        assert len(first_page.items) == 11
        assert all(len(line) <= 62 for line in first_page.items[1:])
        assert first_page.items[1].startswith("● Dele: Opening words")
        assert first_page.hint == "down more · read-only · left x2: back"
        assert first_page.selectable is False
        assert first_page.reader_page == ("hud-reader-conversation", 0, 2)
        assert len(replies.selected) == 1

        # The page gesture is bound to the reader's own trace.
        router.on_input(page_event(EventType.SCROLL_BOTTOM, "hud-other", "messages-full-reader"))
        await asyncio.sleep(0.01)
        assert len(conversation_frames(transport)) == 1

        router.on_input(page_event(
            EventType.SCROLL_BOTTOM, "hud-reader-conversation", "messages-full-reader",
        ))
        await wait_until(lambda: len(conversation_frames(transport)) == 2)
        second_page = conversation_frames(transport)[-1]
        assert second_page.items[0] == "2/2 · Dele"
        assert second_page.hint == "up previous · read-only · left x2: back"
        assert second_page.reader_page == ("hud-reader-conversation", 1, 2)
        assert "".join([*first_page.items[1:], *second_page.items[1:]]) == (
            "● Dele: " + messages.body
        )
        assert len(replies.selected) == 1

        # Past the last page with nothing older: nothing is read or painted.
        await asyncio.sleep(0)
        router.on_input(page_event(
            EventType.SCROLL_BOTTOM, "hud-reader-conversation", "messages-full-reader",
        ))
        await asyncio.sleep(0.01)
        assert len(conversation_frames(transport)) == 2

        router.on_input(InputEvent(
            container_name="capability-lifecycle",
            selected_name="messages-conversation",
        ))
        assert router._message_history is not None
        assert replies.cleared == ["messages-open"]

        router.on_input(InputEvent(
            container_name="capability-lifecycle",
            selected_name="messages-home",
        ))
        assert router._message_history is None
        assert replies.cleared == ["messages-open", "home"]

    @pytest.mark.parametrize("body", [
        pytest.param("x" * 2500 + "FINAL", id="unbroken-value"),
        pytest.param("漢" * 440 + "FINAL", id="wide-cjk"),
        pytest.param("😀" * 320 + "FINAL", id="emoji-exceeds-ten-old-rows"),
        pytest.param("  Opening\r\n\r\n" + "Keep  every\tspace.\r\n" * 35 + "FINAL\n  ",
                     id="exact-whitespace"),
        pytest.param("é 👩‍💻 " * 100 + "FINAL", id="combining-and-joined-emoji"),
    ])
    async def test_thread_reader_preserves_exact_serialized_pages_and_back(
        self, tmp_path, body: str,
    ) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, replies=replies)
        messages = LongHistoryMessages([("Dele", "Preview", time.time())])
        messages.body = body
        router._messages = messages  # type: ignore[assignment]
        request_id = "message-exact-reader"
        await router.open("Messages", request_id=request_id)
        await router._select_capability_row("MESSAGES", 1, "select-thread", request_id)

        pages = []
        for expected in range(100):
            frame = [item for item in transport.sent if isinstance(item, HudList)][-1]
            wire = frame.to_wire()
            assert frame.title == "MESSAGES · CONVERSATION"
            assert frame.request_id == request_id
            assert wire["items"] == frame.items, "body cannot be shortened during serialization"
            assert 2 <= len(frame.items) <= 11
            assert all(len(row.encode("utf-16-le")) // 2 <= 64 for row in frame.items)
            assert all(sum(
                8 if char == "\t" else 2
                if ord(char) > 0xFFFF or unicodedata.east_asian_width(char) in "WF" else 1
                for char in row.rstrip("\r\n")
            ) <= 62 for row in frame.items[1:])
            counter = re.match(r"^(\d+)/(\d+) · Dele$", frame.items[0])
            assert counter is not None, "reader pages must identify their page first"
            index, total = int(counter[1]) - 1, int(counter[2])
            assert index == expected
            assert wire["readerPage"] == {"id": "select-thread", "index": index, "count": total}
            assert ("down more" in (frame.hint or "")) == (index + 1 < total)
            assert ("up previous" in (frame.hint or "")) == (index > 0)
            pages.append(wire)
            if index + 1 == total:
                break
            await router._page_message_history(EventType.SCROLL_BOTTOM, "select-thread", request_id)
        else:
            pytest.fail("thread reader did not expose its final page")

        assert len(pages) > 1
        assert "".join(row for page in pages for row in page["items"][1:]) == "● Dele: " + body
        assert "FINAL" in "".join(pages[-1]["items"][1:])
        assert messages.body == body
        assert len(replies.selected) == 1

        before = len(transport.sent)
        await router._page_message_history(EventType.SCROLL_TOP, "select-thread", "old-request")
        assert len(transport.sent) == before
        await router._page_message_history(EventType.SCROLL_TOP, "forged-trace", request_id)
        assert len(transport.sent) == before
        await router._page_message_history(EventType.SCROLL_TOP, "select-thread", request_id)
        assert transport.sent[-1].to_wire() == pages[-2]
        router.on_input(InputEvent(container_name="capability-lifecycle",
            selected_name="messages-conversation", request_id=request_id))
        assert router._message_history is not None
        assert len(replies.selected) == 1

    async def test_thread_reader_keeps_counter_visible_before_a_long_sender(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path, replies=FakeReplies())
        messages = LongHistoryMessages([("😀" * 50, "Preview", time.time())])
        messages.body = "Exact body."
        router._messages = messages  # type: ignore[assignment]
        await router.open("Messages", request_id="long-sender")
        await router._select_capability_row("MESSAGES", 1, "select-thread", "long-sender")
        frame = [item for item in transport.sent if isinstance(item, HudList)][-1]
        wire = frame.to_wire()
        assert wire["items"][0].startswith("1/1 · 😀")
        assert len(wire["items"][0].encode("utf-16-le")) // 2 <= 64
        # The header is cut on whole code points, never mid-surrogate.
        wire["items"][0].encode("utf-8")
        assert wire["items"][1:] == frame.items[1:]
        assert "".join(wire["items"][1:]) == "● " + "😀" * 50 + ": Exact body."

    async def test_conversation_lines_show_message_metadata_and_older_history_on_the_page(
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
        await wait_until(lambda: bool(conversation_frames(transport)))

        latest = conversation_frames(transport)[-1]
        assert latest.items == [
            "1/1+ · Dele",
            "● Dele: [attachment]\n",
            "You: On my way\n",
            "Dele: Are you coming?",
        ]
        # chat.db has older messages: the reader promises one more page.
        assert latest.hint == "down more · read-only · left x2: back"
        assert latest.reader_page == ("hud-history", 0, 2)
        assert latest.page_controls is None
        await asyncio.sleep(0)

        # Neither a stale request nor another trace can read older history.
        router.on_input(page_event(EventType.SCROLL_BOTTOM, "hud-history", "stale-request"))
        await asyncio.sleep(0.01)
        router.on_input(page_event(EventType.SCROLL_BOTTOM, "hud-forged", "messages-history"))
        await asyncio.sleep(0.01)
        assert messages.page_calls == [None]
        assert len(conversation_frames(transport)) == 1

        router.on_input(page_event(EventType.SCROLL_BOTTOM, "hud-history", "messages-history"))
        await wait_until(lambda: len(conversation_frames(transport)) == 2)
        assert messages.page_calls == [None, 10]
        # The older message fits the same page: it is repainted with it, and
        # without the promise of more.
        current = conversation_frames(transport)[-1]
        assert current.items == [
            "1/1 · Dele",
            "● Dele: [attachment]\n",
            "You: On my way\n",
            "Dele: Are you coming?\n",
            "Dele: Earlier",
        ]
        assert current.hint == "read-only · left x2: back"
        assert current.reader_page == ("hud-history", 0, 1)
        await asyncio.sleep(0)

        router.on_input(page_event(EventType.SCROLL_BOTTOM, "hud-history", "messages-history"))
        await asyncio.sleep(0.01)
        assert messages.page_calls == [None, 10]
        assert len(conversation_frames(transport)) == 2

    async def test_a_swipe_past_the_last_page_reads_older_messages_into_the_next_page(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        messages = OlderHistoryMessages()
        router, transport, _ = make_router(tmp_path, replies=replies)
        router._messages = messages  # type: ignore[assignment]
        await router.open("Messages", request_id="messages-older")
        await router._select_capability_row("MESSAGES", 1, "hud-older", "messages-older")

        first = conversation_frames(transport)[-1]
        assert first.items[0] == "1/1+ · Dele"
        assert first.items[1] == "You: Newest 30 ▶\n"  # the newest own row carries its one-glyph state
        assert first.items[-1] == "Dele: Newest 21"
        assert len(first.items) == 11
        assert first.reader_page == ("hud-older", 0, 2)
        assert "down more" in (first.hint or "")
        assert messages.page_calls == [None]

        router.on_input(page_event(EventType.SCROLL_BOTTOM, "hud-older", "messages-older"))
        await wait_until(lambda: len(conversation_frames(transport)) == 2)
        # Older rows are read from before the oldest row already shown.
        assert messages.page_calls == [None, 21]
        second = conversation_frames(transport)[-1]
        assert second.items == [
            "2/2 · Dele",
            "Dele: Older 20\n",
            "Dele: Older 19\n",
            "Dele: Older 18",
        ]
        assert second.hint == "up previous · read-only · left x2: back"
        assert second.reader_page == ("hud-older", 1, 2)
        assert len(replies.selected) == 1
        await asyncio.sleep(0)

        # Back up to the newest page without another read.
        router.on_input(page_event(EventType.SCROLL_TOP, "hud-older", "messages-older"))
        await wait_until(lambda: len(conversation_frames(transport)) == 3)
        assert conversation_frames(transport)[-1].items[:2] == ["1/2 · Dele", "You: Newest 30 ▶\n"]
        assert messages.page_calls == [None, 21]

    async def test_a_sent_reply_shows_as_the_newest_message_once_chat_db_has_it(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        messages = SentReplyMessages(records_after=3)
        router, transport, _ = make_router(
            tmp_path, replies=FakeReplies(), message_ui=FakeMessageUI(),
        )
        router._messages = messages  # type: ignore[assignment]
        await router.open("Messages", request_id="messages-sent")
        await router._select_capability_row("MESSAGES", 1, "hud-sent", "messages-sent")
        await router._page_message_history(EventType.SCROLL_BOTTOM, "hud-sent", "messages-sent")
        assert conversation_frames(transport)[-1].items[0] == "2/2 · Dele"

        await router.show_sent_reply("On my way!")
        # The first post-send read missed the row; the wait found it.
        assert messages.reads == 3
        frame = conversation_frames(transport)[-1]
        assert frame.request_id == "messages-sent"
        assert frame.items[0] == "1/2 · Dele"
        assert frame.items[1] == "You: On my way! ▶\n"
        assert frame.reader_page == ("hud-sent", 0, 2)
        assert frame.hint == "down more · tap: reply · left x2: back"
        state = router._message_history
        assert state is not None and state.entries[0].row_id == 6
        assert [entry.from_me for entry in state.entries] == [True, False]

    async def test_a_sent_reply_missing_from_chat_db_is_shown_from_the_send(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        import joat.services.capabilities as capabilities
        monkeypatch.setattr(capabilities, "_SENT_REPLY_WAIT_S", 0.05)
        messages = SentReplyMessages(records_after=None)
        router, transport, _ = make_router(
            tmp_path, replies=FakeReplies(), message_ui=FakeMessageUI(),
        )
        router._messages = messages  # type: ignore[assignment]
        await router.open("Messages", request_id="messages-fallback")
        await router._select_capability_row("MESSAGES", 1, "hud-fallback", "messages-fallback")
        await router._page_message_history(EventType.SCROLL_BOTTOM, "hud-fallback", "messages-fallback")

        await router.show_sent_reply("  Running late  ")
        assert messages.reads > 1
        frame = conversation_frames(transport)[-1]
        assert frame.request_id == "messages-fallback"
        assert frame.items[0] == "1/2 · Dele"
        assert frame.items[1] == "You:   Running late   ▶\n"
        assert frame.items[2].startswith("Dele: word word")
        assert frame.reader_page == ("hud-fallback", 0, 2)
        state = router._message_history
        assert state is not None
        assert state.entries[0].from_me is True and state.entries[0].text == "  Running late  "
        assert len(state.entries) == 2

    async def test_a_sent_reply_does_not_paint_after_the_wearer_leaves_the_thread(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        messages = SentReplyMessages(records_after=None)
        router, transport, _ = make_router(
            tmp_path, replies=FakeReplies(), message_ui=FakeMessageUI(),
        )
        router._messages = messages  # type: ignore[assignment]
        await router.open("Messages", request_id="messages-left")
        await router._select_capability_row("MESSAGES", 1, "hud-left", "messages-left")
        pending = asyncio.create_task(router.show_sent_reply("Hello"))
        await wait_until(lambda: messages.reads >= 2)
        router.on_input(InputEvent(
            container_name="capability-lifecycle", selected_name="messages-list",
            request_id="messages-left",
        ))
        before = len(transport.sent)
        await asyncio.wait_for(pending, 5)
        assert len(transport.sent) == before
        assert router._message_history is None

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

    async def test_picker_back_disarms_reply_but_allows_second_selection(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, replies=replies)
        router._messages = PagedHistoryMessages()  # type: ignore[assignment]
        await router.open("Messages", request_id="picker-return")
        select = InputEvent(container_name="capdata", selected_name="MESSAGES",
                            selected_index=1, request_id="picker-return")
        router.on_input(select)
        await wait_until(lambda: len(replies.selected) == 1)
        router.on_input(InputEvent(container_name="capability-lifecycle",
                                   selected_name="messages-list"))
        assert replies.cleared == ["messages-open", "list"]
        before = len(transport.sent)
        router.on_input(select)
        await asyncio.sleep(.05)
        assert len(replies.selected) == 2
        assert any(isinstance(m, HudList) and m.title == "MESSAGES · CONVERSATION"
                   for m in transport.sent[before:])

    @pytest.mark.parametrize("boundary", ["messages-list", "messages-home", "disconnect"])
    async def test_pending_message_selection_cannot_rearm_after_context_ends(
        self, tmp_path, boundary: str,
    ) -> None:  # type: ignore[no-untyped-def]
        messages = HeldHistoryMessages()
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, replies=replies)
        router._messages = messages  # type: ignore[assignment]
        await router.open("Messages", request_id="held-selection")
        pending = asyncio.create_task(router._select_capability_row(
            "MESSAGES", message_row_index(transport, "] Thread A ·"),
            "held-selection", "held-selection",
        ))
        try:
            assert await asyncio.to_thread(messages.started.wait, 1)
            if boundary == "disconnect":
                router.on_disconnect()
            else:
                router.on_input(InputEvent(
                    container_name="capability-lifecycle", selected_name=boundary,
                ))
            before = list(transport.sent)
        finally:
            messages.release.set()
            await pending

        assert replies.selected == []
        assert router._message_selection_active is False
        assert router._message_history is None
        assert transport.sent == before

    @pytest.mark.parametrize("error", [
        pytest.param(PermissionError("synthetic old page failure"), id="failure"),
        pytest.param(None, id="late-success"),
    ])
    async def test_old_message_page_failure_cannot_clear_new_selection(
        self, tmp_path, error: OSError | None,
    ) -> None:  # type: ignore[no-untyped-def]
        messages = HeldHistoryMessages(hold_older=True, error=error)
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, replies=replies)
        router._messages = messages  # type: ignore[assignment]
        await router.open("Messages", request_id="held-page")
        first_index = message_row_index(transport, "] Thread A ·")
        second_index = message_row_index(transport, "] Thread B ·")
        await router._select_capability_row("MESSAGES", first_index, "first", "held-page")
        # Thread A has older history; the swipe past its only page reads it.
        assert conversation_frames(transport)[-1].reader_page == ("first", 0, 2)
        pending = asyncio.create_task(router._page_message_history(
            EventType.SCROLL_BOTTOM, "first", "held-page",
        ))
        try:
            assert await asyncio.to_thread(messages.started.wait, 1)
            router.on_input(InputEvent(
                container_name="capability-lifecycle", selected_name="messages-list",
            ))
            await router._select_capability_row("MESSAGES", second_index, "second", "held-page")
            assert replies.selected[-1][0].sender == "Thread B"
            before = list(transport.sent)
        finally:
            messages.release.set()
            await pending

        assert replies.cleared == ["messages-open", "list"]
        assert router._message_selection_active is True
        assert router._message_history is not None
        assert router._message_history.thread.sender == "Thread B"
        assert [entry.row_id for entry in router._message_history.entries] == [10]
        assert router._message_history.index == 0
        assert transport.sent == before

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

        assert replies.cleared == ["messages-open", "home"]

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

        assert replies.cleared == ["messages-open", "messages-open"]
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

        assert replies.cleared == ["messages-open", "disconnect"]

    @pytest.mark.parametrize("boundary", ["messages-home", "menu-home", "disconnect"])
    @pytest.mark.parametrize("stage", ["awaiting_input", "awaiting_confirmation"])
    async def test_home_and_disconnect_cancel_typed_message_without_picker_selection(
        self, tmp_path, boundary: str, stage: str,
    ) -> None:  # type: ignore[no-untyped-def]
        sent: list[tuple[str, str]] = []
        confirmation_started = asyncio.Event()
        release_confirmation = asyncio.Event()

        async def send(recipient: str, body: str) -> None:
            sent.append((recipient, body))

        async def confirm(
            _action_id: str, _prompt: str, _timeout_s: float, **_binding: str,
        ) -> bool:
            confirmation_started.set()
            await release_confirmation.wait()
            return True

        replies = MessageReplyController(
            SimpleNamespace(send=send),  # type: ignore[arg-type]
            confirm,
            contact_search=lambda _query: [("Synthetic Contact", "+12025550123")],
        )
        router, _, _ = make_router(tmp_path, replies=replies)
        action_id = f"typed-{boundary}-{stage}"
        try:
            prepared = await replies.prepare_new_message(
                action_id=action_id, trace_id="synthetic-conversation",
                recipient_name="Synthetic Contact",
                body="Synthetic message." if stage == "awaiting_confirmation" else None,
            )
            assert prepared.state == stage
            assert router._message_selection_active is False
            if stage == "awaiting_confirmation":
                await asyncio.wait_for(confirmation_started.wait(), timeout=1)

            if boundary == "disconnect":
                router.on_disconnect()
            elif boundary == "menu-home":
                router.on_input(InputEvent(container_name="menu", selected_name="closed"))
            else:
                router.on_input(InputEvent(
                    container_name="capability-lifecycle", selected_name="messages-home",
                ))
            await asyncio.sleep(0)
            state_after_boundary = replies.message_action_status(action_id).state

            # A delayed input or approval from the retired context cannot send.
            if stage == "awaiting_input":
                await replies.provide_message_input(action_id, body="Synthetic message.")
            release_confirmation.set()
            await wait_until(lambda: replies.message_action_status(action_id).state
                             not in {"awaiting_input", "awaiting_confirmation", "sending"})

            assert state_after_boundary == "cancelled"
            assert replies.message_action_status(action_id).state == "cancelled"
            assert sent == []
        finally:
            release_confirmation.set()
            await replies.aclose()

    async def test_group_message_cannot_be_misrouted_to_one_participant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Group identity is independent of reply support: a group with no
        # chat GUID has no send target, so it reads as "Participant" and
        # never arms a reply to whichever member happened to write last,
        # even with a message UI that could take a tap.
        thread = MessageThread("Project team", "handle-0", "Ship it", time.time(), is_group=True)
        monkeypatch.setattr(FakeMessages, "recent_threads", lambda _self, limit=5: [thread])
        replies = FakeReplies()
        message_ui = FakeMessageUI()
        router, transport, _ = make_router(
            tmp_path, rows=[], replies=replies, message_ui=message_ui,
        )
        await router.open("Messages")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Project team"),
            trace_id="hud-group",
        ))
        await wait_until(lambda: bool(conversation_frames(transport)))

        assert replies.selected == []
        frame = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert frame.selectable is False
        assert frame.items == ["1/1 · Project team (group)", "Participant: Ship it"]
        assert frame.hint == "read-only · left x2: back"
        assert "say reply" not in frame.hint
        assert "conversation_ready" not in message_ui.calls
        assert router._message_selection_active is False
        assert thread.handle not in repr(transport.sent)

    async def test_group_with_chat_guid_arms_reply_and_keeps_identity_mac_side(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        thread = MessageThread(
            "Project team", "private-latest-participant", "Ship it", time.time(),
            chat_guid="iMessage;+;private-group-target", is_group=True,
        )
        monkeypatch.setattr(FakeMessages, "recent_threads", lambda _self, limit=5: [thread])
        replies = FakeReplies()
        message_ui = FakeMessageUI()
        router, transport, _ = make_router(
            tmp_path, rows=[], replies=replies, message_ui=message_ui,
        )
        await router.open("Messages")
        router.on_input(InputEvent(
            container_name="capdata", selected_name="MESSAGES",
            selected_index=message_row_index(transport, "] Project team"), trace_id="group-guid",
        ))
        await wait_until(lambda: bool(replies.selected))
        await wait_until(lambda: bool(conversation_frames(transport)))

        assert replies.selected == [(thread, "group-guid")]
        frame = [message for message in transport.sent if isinstance(message, HudList)][-1]
        assert frame.hint == "tap: reply · left x2: back"
        assert "read-only" not in frame.hint
        assert frame.selectable is True
        assert frame.items == ["1/1 · Project team (group)", "Participant: Ship it"]
        assert frame.reader_page == ("group-guid", 0, 1)

        # A tap replies to the whole group: the target never crosses the
        # wire and the tap cannot re-select a participant.
        router.on_input(InputEvent(
            container_name="capmessage", selected_index=1, trace_id="group-guid",
        ))
        await wait_until(lambda: "begin_reply" in message_ui.calls)
        assert replies.selected == [(thread, "group-guid")]
        wire_frames = repr(transport.sent) + repr([m.to_wire() for m in transport.sent
                                                   if hasattr(m, "to_wire")])
        assert thread.handle not in wire_frames
        assert thread.chat_guid not in wire_frames

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
        # One footer: the Back copy lives in the hint, not duplicated as a row
        # (Changelog 2026-09-11, "duplicated Back copy on event details").
        assert detail.items == [
            "Design review",
            "09:30 · Event",
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
        assert note_list.items[:2] == NOTE_CONTROL_ROWS
        assert "Ask Adaeze" in note_list.items[2]
        assert len(note_list.items) == 3
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="QUICKNOTES",
            selected_index=2,
        ))
        await asyncio.sleep(0)
        note_detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert note_detail.title == "QUICKNOTES · DETAIL"
        assert note_detail.items[0] == "1/1 · Exact text"
        assert "".join(note_detail.to_wire()["items"][1:]) == "Ask Adaeze about the design review"

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

    @pytest.mark.parametrize(("body", "first_row"), [
        pytest.param("A" * 62 + "Z" * 8 + "\r\n", "A" * 62, id="long-word"),
        pytest.param("word " * 30, "word " * 12, id="whole-words"),
        pytest.param("漢" * 31 + "字" * 4 + "\r\n", "漢" * 31, id="wide-cjk"),
        pytest.param("😀" * 31 + "🙂" * 4 + "\r\n", "😀" * 31, id="surrogate-pairs"),
        pytest.param("C" * 62 + "\r\n", "C" * 62 + "\r\n", id="crlf-reservation"),
        pytest.param("A" * 61 + "e\u0301" + "Z" * 5 + "\r\n", "A" * 61,
                     id="combining-boundary"),
        pytest.param("A" * 59 + "👩\u200d💻" + "Z" * 5 + "\r\n", "A" * 59,
                     id="joined-emoji-boundary"),
        pytest.param("\t" * 10 + "FINAL", "\t" * 7, id="conservative-tabs"),
    ])
    async def test_quicknote_reader_uses_expanded_width_without_wire_loss(
        self, tmp_path, body: str, first_row: str,
    ) -> None:  # type: ignore[no-untyped-def]
        """The 548px reader uses up to 62 mono columns without SDK clipping."""
        with DurableStore(tmp_path / "wide-note.sqlite3") as durable:
            notes = NoteStore(durable)
            notes.create(note_id="wide", idempotency_key="wide", text=body,
                         source=NoteSource(kind="synthetic"), now_us=10)
            router, transport, _ = make_router(tmp_path, notes=notes)
            await router.open("QuickNotes", request_id="wide-note-reader")
            await router._select_capability_row(
                "QUICKNOTES", 2, "reader-wide", "wide-note-reader",
            )
            pages = await collect_note_reader(
                router, transport, request_id="wide-note-reader", trace_id="reader-wide",
            )
            rows = [row for page in pages for row in page["items"][1:]]
            assert rows[0] == first_row, "reader still leaves the available width unused"
            assert "".join(rows) == body
            assert notes.get("wide").text == body
            assert all(not row.startswith(("\u0301", "\u200d")) for row in rows)
            assert all(not row.endswith("\u200d") for row in rows)

    async def test_quicknote_reader_preserves_full_exact_wire_text_and_back_selection(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        body = (
            "  Opening words.\r\n\r\n" + "漢" * 60 + "\n" + "😀" * 30 + "\r\n"
            + "e\u0301 👩\u200d💻 " * 18 + "\n" + "unbroken" * 35 + "\n"
            + "Keep  both spaces. " * 60 + "FINAL-TEXT-MARKER\n  "
        )
        second_body = "Second note " + "second-body " * 60
        with DurableStore(tmp_path / "notes.sqlite3") as durable:
            notes = NoteStore(durable)
            for identifier, text, at in (("long", body, 10), ("second", second_body, 20)):
                notes.create(note_id=identifier, idempotency_key=identifier, text=text,
                             source=NoteSource(kind="synthetic"), now_us=at)
            router, transport, _ = make_router(tmp_path, notes=notes)
            await router.open("QuickNotes", request_id="note-reader")
            await router._select_capability_row("QUICKNOTES", 3, "reader-long", "note-reader")
            pages = await collect_note_reader(router, transport, request_id="note-reader", trace_id="reader-long")
            assert len(pages) > 2
            assert "".join(row for page in pages for row in page["items"][1:]) == body
            assert notes.get("long").text == body

            before = len(transport.sent)
            router.on_input(InputEvent(container_name="cappage", selected_name="QUICKNOTES",
                selected_index=len(pages) - 1, event_type=EventType.SCROLL_TOP,
                request_id="old-request", trace_id="reader-long"))
            await asyncio.sleep(0)
            assert len(transport.sent) == before
            router.on_input(InputEvent(container_name="cappage", selected_name="QUICKNOTES",
                selected_index=len(pages) - 1, event_type=EventType.SCROLL_TOP,
                request_id="note-reader", trace_id="reader-long"))
            await wait_until(lambda: len(transport.sent) > before)
            assert transport.sent[-1].to_wire() == pages[-2]

            before = len(transport.sent)
            router.on_input(InputEvent(container_name="cappage", selected_name="QUICKNOTES",
                selected_index=len(pages) - 2, event_type=EventType.SCROLL_TOP,
                request_id="note-reader", trace_id="reader-long"))
            router.on_input(InputEvent(container_name="capability-lifecycle",
                selected_name="quicknotes-list", request_id="note-reader"))
            await asyncio.sleep(0)
            assert len(transport.sent) == before, "Back must retire an already queued page"
            await router._select_capability_row("QUICKNOTES", 2, "reader-second", "note-reader")
            second = [m for m in transport.sent if isinstance(m, HudList)][-1].to_wire()
            assert "".join(second["items"][1:]).startswith("Second note")
            assert "down more" in second["hint"]
            before = len(transport.sent)
            # An old reader's page zero cannot advance a newly opened note.
            router.on_input(InputEvent(container_name="cappage", selected_name="QUICKNOTES",
                selected_index=0, event_type=EventType.SCROLL_BOTTOM,
                request_id="note-reader", trace_id="reader-long"))
            await asyncio.sleep(0)
            assert len(transport.sent) == before

    @pytest.mark.parametrize("with_summary", [True, False])
    async def test_quicknote_reader_reaches_complete_recording_transcript(
        self, tmp_path, with_summary: bool,
    ) -> None:  # type: ignore[no-untyped-def]
        first = "First section\n\n" + ("Keep exact  words. " * 40).rstrip()
        last = "Last section " + "unbroken" * 40 + " FINAL-RECORDING-MARKER"
        with DurableStore(tmp_path / "recording.sqlite3") as durable:
            notes = VoiceNoteStore(durable)
            notes.create(capture_id="recording", idempotency_key="recording",
                         title="Synthetic recording", source_kind="synthetic", now_us=10)
            notes.transition("recording", "recording", now_us=11)
            for seq in range(2):
                notes.append_frame("recording", connection_generation="synthetic", seq=seq,
                    source="glasses", speaker="wearer", pcm=b"\x01\x00", received_at_us=12 + seq)
            notes.append_gap("recording", code="synthetic_gap", detail="Missing interval", now_us=14)
            notes.transition("recording", "finalizing", now_us=15)
            for ordinal, text in enumerate((first, last)):
                notes.append_transcript("recording", text=text, first_frame_ordinal=ordinal,
                    last_frame_ordinal=ordinal, now_us=16 + ordinal)
            notes.finalize("recording", complete=False, completion_code="incomplete_raw_retained", now_us=20)
            if with_summary:
                notes.replace_insights("recording", engine="synthetic", summary="Summary only.",
                    key_points=("One point",), todos=("One action",), now_us=21)
            router, transport, _ = make_router(tmp_path, voice_notes=notes)
            await router.open("QuickNotes", request_id="recording-reader")
            await router._select_capability_row("QUICKNOTES", 2, "reader-recording", "recording-reader")
            pages = await collect_note_reader(router, transport, request_id="recording-reader", trace_id="reader-recording")
            body = "".join(row for page in pages for row in page["items"][1:])
            assert first in body and last in body
            assert body.index(first) < body.index(last)
            assert "[synthetic_gap] Missing interval" in body
            assert ("Summary only." in body) == with_summary
            assert "FINAL-RECORDING-MARKER" in "".join(pages[-1]["items"][1:])

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
        # The two control rows lead; then recordings (marked with the ●
        # record glyph), then exact dictated text notes.
        assert note_list.items[:2] == NOTE_CONTROL_ROWS
        assert "● Standup notes" in note_list.items[2]
        assert "Ask Adaeze" in note_list.items[3]
        assert len(note_list.items) == 4
        assert note_list.selectable is True

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="QUICKNOTES",
            selected_index=2,
        ))
        await asyncio.sleep(0)
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "QUICKNOTES · NOTE"
        recording_pages = await collect_note_reader(router, transport, request_id=None, trace_id="hud")
        recording_text = "".join(row for page in recording_pages for row in page["items"][1:])
        assert recording_text.startswith("Standup notes\n")
        assert "OVERVIEW\nThe team agreed to ship Friday." in recording_text
        assert "KEY POINTS" in recording_text and "NEXT STEPS" in recording_text
        # Q&A remains discoverable in the body; the footer owns page/Back gestures.
        assert "ask the last note" in recording_text
        assert "TRANSCRIPT\nWe agreed to ship Friday." in recording_text

        # Text rows keep their exact-text detail behaviour.
        await router.open("QuickNotes")
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="QUICKNOTES",
            selected_index=3,
        ))
        await asyncio.sleep(0)
        text_detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert text_detail.title == "QUICKNOTES · DETAIL"
        assert text_detail.items[0] == "1/1 · Exact text"
        assert "".join(text_detail.to_wire()["items"][1:]) == "Ask Adaeze about the design review"

    async def test_an_open_note_owns_the_quicknotes_page_as_its_live_words(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """FEAT-002: the open note is a live text page — its words, a tap to
        pause, 2x tap for the finish rows."""
        store = DurableStore(tmp_path / "durable.sqlite3")
        voice_notes = VoiceNoteStore(store)
        _retained_voice_note(voice_notes, "capture-0", base_us=5)
        capture = FakeVoiceCapture(state="recording", title="Client call")
        capture.lines = (LiveLine("We ship on Friday.", "other"), LiveLine("Agreed.", "self"))
        capture.recorded_s = 75
        router, transport, _ = make_router(
            tmp_path, voice_notes=voice_notes, voice_capture=capture,
        )

        await router.open("QuickNotes", request_id="notes-live")
        live = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert live.title == "QUICKNOTES · LIVE"
        assert "".join(live.items) == "Them: We ship on Friday.\nYou: Agreed."
        assert live.selectable is True
        assert live.hint == "● REC 01:15 · tap: pause · left x2: finish"

        # A tap is the pause/resume toggle, never a row selection.
        router.on_input(InputEvent(
            container_name="capdata", selected_name="QUICKNOTES",
            selected_index=0, request_id="notes-live",
        ))
        await wait_until(lambda: capture.calls == ["toggle"])
        await wait_until(lambda: [m for m in transport.sent if isinstance(m, HudList)][-1].hint
                         == "01:15 · hold: talk · tap: record · left x2: finish")
        assert receipts(transport) == []

        # 2x tap asks what to do with it; Save saves and returns to the list.
        router.on_input(InputEvent(
            container_name="capability-lifecycle", selected_name="quicknotes-finish",
            request_id="notes-live",
        ))
        await wait_until(lambda: [m for m in transport.sent if isinstance(m, HudList)][-1].title
                         == "QUICKNOTES · FINISH")
        finish = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert finish.items == ["Save note", "Keep recording", "Discard note"]
        router.on_input(InputEvent(
            container_name="capdata", selected_name="QUICKNOTES",
            selected_index=0, request_id="notes-live",
        ))
        await wait_until(lambda: "stop" in capture.calls)
        await wait_until(lambda: [m for m in transport.sent if isinstance(m, HudList)][-1].title
                         == "QUICKNOTES")
        assert any(
            isinstance(m, AssistantOverlay) and m.text == "Saving quicknote…"
            for m in transport.sent
        )
        after = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert after.items[:2] == NOTE_CONTROL_ROWS
        assert "● Standup notes" in after.items[2]
        store.close()

    async def test_keep_recording_on_the_finish_rows_returns_to_the_note(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        capture = FakeVoiceCapture(state="recording", title="Standup")
        router, transport, _ = make_router(tmp_path, voice_capture=capture)
        await router.open("QuickNotes", request_id="notes-keep")
        router.on_input(InputEvent(container_name="capability-lifecycle",
                                   selected_name="quicknotes-finish", request_id="notes-keep"))
        await wait_until(lambda: [m for m in transport.sent if isinstance(m, HudList)][-1].title
                         == "QUICKNOTES · FINISH")

        # Both ways back: the Keep row, and 2x tap on the rows.
        router.on_input(InputEvent(container_name="capdata", selected_name="QUICKNOTES",
                                   selected_index=1, request_id="notes-keep"))
        await wait_until(lambda: [m for m in transport.sent if isinstance(m, HudList)][-1].title
                         == "QUICKNOTES · LIVE")
        router.on_input(InputEvent(container_name="capability-lifecycle",
                                   selected_name="quicknotes-finish", request_id="notes-keep"))
        await wait_until(lambda: [m for m in transport.sent if isinstance(m, HudList)][-1].title
                         == "QUICKNOTES · FINISH")
        router.on_input(InputEvent(container_name="capability-lifecycle",
                                   selected_name="quicknotes-back", request_id="notes-keep"))
        await wait_until(lambda: [m for m in transport.sent if isinstance(m, HudList)][-1].title
                         == "QUICKNOTES · LIVE")
        assert "stop" not in capture.calls

    async def test_a_held_touchpad_talks_into_a_quick_note(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        capture = FakeVoiceCapture(state="idle", title="Note 09:30", active=False)
        with DurableStore(tmp_path / "notes.sqlite3") as durable:
            router, transport, _ = make_router(
                tmp_path, notes=NoteStore(durable), voice_capture=capture,
            )
            await router.open("QuickNotes", request_id="notes-hold")
            router.on_input(InputEvent(container_name="capdata", selected_name="QUICKNOTES",
                                       selected_index=1, request_id="notes-hold"))
            await wait_until(lambda: capture.calls == ["start:hold"])
            await wait_until(lambda: [m for m in transport.sent if isinstance(m, HudList)][-1].title
                             == "QUICKNOTES · LIVE")
            opened = [m for m in transport.sent if isinstance(m, HudList)][-1]
            assert "Hold the touchpad and speak" in "".join(opened.items)

            router.on_input(InputEvent(container_name="capability-lifecycle",
                                       selected_name="quicknotes-hold-start", request_id="notes-hold"))
            await wait_until(lambda: capture.calls[-1] == "hold_start")
            await wait_until(lambda: (
                [m for m in transport.sent if isinstance(m, HudList)][-1].hint or ""
            ).startswith("● Listening"))
            router.on_input(InputEvent(container_name="capability-lifecycle",
                                       selected_name="quicknotes-hold-end", request_id="notes-hold"))
            await wait_until(lambda: capture.calls[-1] == "hold_end")
            assert capture.snapshot.state == "paused"
            # A gesture from an older visit is not this page's.
            router.on_input(InputEvent(container_name="capability-lifecycle",
                                       selected_name="quicknotes-hold-start", request_id="notes-old"))
            await asyncio.sleep(0.05)
            assert capture.calls[-1] == "hold_end"

    async def test_the_live_page_asks_for_the_mic_when_no_audio_arrives(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        capture = FakeVoiceCapture(state="recording", title="Standup")
        capture.silent_s = 3.5
        router, transport, _ = make_router(tmp_path, voice_capture=capture)
        await router.open("QuickNotes", request_id="notes-silent")
        live = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert "No audio from the mic" in "".join(live.items)

        await router._live_check(tick=True)

        assert any(isinstance(m, AudioControl) and m.open for m in transport.sent)
        before = sum(isinstance(m, AudioControl) for m in transport.sent)
        await router._live_check(tick=True)
        assert sum(isinstance(m, AudioControl) for m in transport.sent) == before, "asks once"

    async def test_the_apple_notes_row_flips_the_switch(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        class Switch:
            enabled = True

            def set_enabled(self, enabled: bool) -> None:
                self.enabled = enabled

        switch = Switch()
        with DurableStore(tmp_path / "notes.sqlite3") as durable:
            router, transport, _ = make_router(tmp_path, notes=NoteStore(durable))
            router._notes_sync = switch  # type: ignore[assignment]
            await router.open("QuickNotes", request_id="notes-sync")
            page = [m for m in transport.sent if isinstance(m, HudList)][-1]
            assert page.items == [*NOTE_CONTROL_ROWS, "Save to Apple Notes: On"]

            await router._select_capability_row("QUICKNOTES", 2, "hud", "notes-sync")

            assert switch.enabled is False
            assert receipts(transport) == ["Saved notes now stay on this Mac."]
            page = [m for m in transport.sent if isinstance(m, HudList)][-1]
            assert page.items[2] == "Save to Apple Notes: Off"

    async def test_a_generated_title_reaches_the_open_list(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = DurableStore(tmp_path / "durable.sqlite3")
        voice_notes = VoiceNoteStore(store)
        _retained_voice_note(voice_notes, "capture-1", base_us=20)
        router, transport, _ = make_router(tmp_path, voice_notes=voice_notes)
        await router.open("QuickNotes", request_id="notes-rename")
        voice_notes.rename("capture-1", "Delivery and van booking", now_us=50)

        await router.refresh_notes_list()

        listing = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert "● Delivery and van booking" in listing.items[2]
        assert listing.request_id == "notes-rename"
        # Not on the list (a note's page is open): nothing is repainted.
        await router._select_capability_row("QUICKNOTES", 2, "rec", "notes-rename")
        before = len(transport.sent)
        await router.refresh_notes_list()
        assert len(transport.sent) == before
        store.close()

    async def test_a_recordings_page_tap_adds_its_next_steps_to_reminders(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        asked: list[str] = []

        async def reminders(capture_id: str) -> str:
            asked.append(capture_id)
            return "Created 1 of 1 reminder from “Standup notes”."

        store = DurableStore(tmp_path / "durable.sqlite3")
        voice_notes = VoiceNoteStore(store)
        _retained_voice_note(voice_notes, "capture-1", base_us=20)
        voice_notes.replace_insights(
            "capture-1", engine="mlx-local", summary="Ship Friday.",
            key_points=(), todos=("Email release notes",), now_us=99,
        )
        router, transport, _ = make_router(tmp_path, voice_notes=voice_notes)
        router._note_reminders = reminders
        await router.open("QuickNotes", request_id="notes-remind")
        await router._select_capability_row("QUICKNOTES", 2, "rec", "notes-remind")
        page = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert page.title == "QUICKNOTES · NOTE"
        assert page.hint is not None and "tap: next steps to Reminders" in page.hint
        assert page.selectable is True

        router.on_input(InputEvent(container_name="capdata", selected_name="QUICKNOTES",
                                   selected_index=0, request_id="notes-remind"))
        await wait_until(lambda: asked == ["capture-1"])
        await wait_until(lambda: "Created 1 of 1 reminder" in " ".join(receipts(transport)))
        store.close()

    async def test_quicknotes_empty_and_setup_pages_still_offer_the_controls(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """No notes yet is a page with two things to tap; no store is a fact."""
        with DurableStore(tmp_path / "empty-notes.sqlite3") as durable:
            router, transport, _ = make_router(tmp_path, notes=NoteStore(durable))
            await router.open("QuickNotes", request_id="notes-empty")
            empty = [m for m in transport.sent if isinstance(m, HudList)][-1]
            assert empty.title == "QUICKNOTES · EMPTY"
            assert empty.items == NOTE_CONTROL_ROWS
            assert empty.hint == "tap: choose · left x2: back"
            assert empty.selectable is True
            assert empty.quick_notes_page == (0, 1)
            assert empty.page_controls == (None, None)

        router, transport, _ = make_router(tmp_path)
        await router.open("QuickNotes", request_id="notes-setup")
        setup = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert setup.title == "QUICKNOTES · SETUP"
        assert setup.items == ["Notes storage is unavailable on this Mac."]
        assert setup.selectable is False

    async def test_the_record_row_opens_a_recording_on_its_live_page(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = DurableStore(tmp_path / "durable.sqlite3")
        voice_notes = VoiceNoteStore(store)
        _retained_voice_note(voice_notes, "capture-0", base_us=5)
        capture = FakeVoiceCapture(state="idle", title="Client call", active=False)
        router, transport, _ = make_router(
            tmp_path, voice_notes=voice_notes, voice_capture=capture,
        )
        await router.open("QuickNotes", request_id="notes-record")
        listing = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert listing.title == "QUICKNOTES"
        assert listing.items[:2] == NOTE_CONTROL_ROWS
        assert "● Standup notes" in listing.items[2]

        router.on_input(InputEvent(
            container_name="capdata", selected_name="QUICKNOTES",
            selected_index=0, request_id="notes-record",
        ))
        await wait_until(lambda: capture.calls == ["start:continuous"])
        await wait_until(lambda: any(
            isinstance(m, HudList) and m.title == "QUICKNOTES · LIVE"
            for m in transport.sent
        ))
        # An opened note shows itself; only a refusal would earn a band.
        assert receipts(transport) == []
        live = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert "Listening — your words appear here." in "".join(live.items)
        store.close()

    async def test_the_record_row_without_a_capture_controller_says_so(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with DurableStore(tmp_path / "notes.sqlite3") as durable:
            router, transport, _ = make_router(tmp_path, notes=NoteStore(durable))
            await router.open("QuickNotes", request_id="notes-no-capture")
            await router._select_capability_row("QUICKNOTES", 0, "hud", "notes-no-capture")
            assert receipts(transport) == ["Recording is unavailable on this Mac."]

    async def test_discard_needs_its_own_confirm_row(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        capture = FakeVoiceCapture(
            state="paused", title="Risky", discard_pending=True,
        )
        router, transport, _ = make_router(tmp_path, voice_capture=capture)

        await router.open("QuickNotes")
        controls = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert controls.title == "QUICKNOTES · DISCARD?"
        assert controls.items == ["Confirm discard", "Keep recording"]

    async def test_quicknotes_discard_back_keeps_then_saves_the_recording(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        capture = FakeVoiceCapture(state="recording", discard_pending=True)
        router, transport, _ = make_router(tmp_path, voice_capture=capture)
        await router.open("QuickNotes", request_id="notes-current")
        router.on_input(InputEvent(container_name="capability-lifecycle",
                                   selected_name="quicknotes-back", request_id="notes-current"))
        await asyncio.sleep(.05)
        assert capture.calls == ["keep"]
        controls = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert controls.title == "QUICKNOTES · LIVE"
        router.on_input(InputEvent(container_name="capability-lifecycle",
                                   selected_name="quicknotes-finish", request_id="notes-current"))
        await asyncio.sleep(.05)
        router.on_input(InputEvent(container_name="capdata", selected_name="QUICKNOTES",
                                   selected_index=0, request_id="notes-current"))
        await asyncio.sleep(.05)
        assert capture.calls == ["keep", "stop"]
        assert not capture.active

    async def test_quicknotes_back_rejects_an_old_request(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        capture = FakeVoiceCapture(discard_pending=True)
        router, transport, _ = make_router(tmp_path, voice_capture=capture)
        await router.open("QuickNotes", request_id="notes-current")
        before = len(transport.sent)
        router.on_input(InputEvent(container_name="capability-lifecycle",
                                   selected_name="quicknotes-back", request_id="notes-old"))
        await asyncio.sleep(.05)
        assert capture.calls == []
        assert len(transport.sent) == before


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
        assert control_list.title == "QUICKNOTES · LIVE"
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

    def test_multi_line_preview_is_one_row_line(self) -> None:
        # A line break in the preview wrapped the firmware row into the next.
        row = CapabilityRouter._message_list_row(MessageThread(
            "Adele", "handle-1", "Running late\n\nsee you  at 3", time.time(), True,
        ))
        assert row.endswith("· Running late see you at 3")


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
        # A hand-opened picker is tapped, not spoken to.
        assert lists[-1].hint == "tap: open · left x2: back"

    async def test_a_session_that_cannot_reopen_points_at_the_new_row(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        """The failure page names the row to tap, not a phrase to say."""
        rows = [self.Past("ccc", "no transcript kept", ())]
        attempted: list[str] = []

        async def continue_it(session_id: str) -> bool:
            attempted.append(session_id)
            return False

        router, transport, _ = make_router(
            tmp_path, brainstorm_sessions=lambda: rows, brainstorm_continue=continue_it,
        )
        await router.open("Brainstorm")
        await router._select_brainstorm(1)
        assert attempted == ["ccc"]
        failed = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert failed.title == "BRAINSTORM"
        assert failed.items == [
            "That session could not be reopened.",
            "Tap “+ New brainstorm” to start another.",
        ]
        assert failed.selectable is False

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

        router, transport, _s = make_router(
            tmp_path, brainstorm_sessions=lambda: rows,
            brainstorm_continue=continue_it)
        await router.open("Brainstorm")
        await router._select_brainstorm(2)      # row 0 is NEW

        # Read first (the audit's B3): the tap opens the saved transcript in
        # the exact reader; continuation — two subscription CLIs — starts
        # only on the next, deliberate tap.
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists[-1].title == "BRAINSTORM · SAVED"
        assert opened == [], "a first tap reads; it must not start collaborators"
        await router._select_brainstorm(2)
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

        await router._select_brainstorm(2)      # opens the reader for "bbb"
        await router._select_brainstorm(2)      # the reader's tap: continue
        assert opened == ["bbb"], "resolved against what was DRAWN"


class TestATerminalReplyTooLongForTheCheckIsReadWhole:
    """2026-10-06: a spoken reply to a work session is checked before it
    goes; one too long for the confirmation page is read in full first."""

    async def test_it_is_sent_only_by_a_tap_on_its_last_page(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)
        words = " ".join(f"step{i} of the long instruction" for i in range(40))
        decision = asyncio.create_task(router.review_terminal_input("work", words, timeout_s=5))
        await wait_until(lambda: router._note_reader is not None)
        reader = router._note_reader
        assert reader is not None and len(reader.pages) > 1
        first = [m for m in transport.sent if isinstance(m, HudList)][-1]
        # The Hub keys a page to its capability by the title's family.
        assert first.title == "AGENT TERMINAL · CHECK"
        assert first.selectable is False and "tap: send" not in first.hint

        reader.index = len(reader.pages) - 1
        await router._show_note_reader()
        last = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert last.selectable is True and "tap: send" in last.hint
        router.on_input(InputEvent(
            container_name="capdata", selected_name="AGENT TERMINAL",
            selected_index=reader.index, request_id=router._active_request_id,
        ))
        assert await asyncio.wait_for(decision, 2) is True

    async def test_no_answer_is_none_and_sends_nothing(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, _transport, _ = make_router(tmp_path)
        words = " ".join(f"step{i} of the long instruction" for i in range(40))
        assert await router.review_terminal_input("work", words, timeout_s=0.3) is None


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

    async def test_explicit_entry_always_shows_agent_choices_even_while_busy(self, tmp_path):
        painted = []
        async def view():
            painted.append("live")
        rows = [("openclaw", "OpenClaw · Working"), ("hermes", "Hermes · Ready")]
        router, transport, _ = make_router(
            tmp_path, terminal_rows=lambda: rows, terminal_view=view,
            terminal_busy=lambda: True)
        await router.open("Agent Terminal")
        assert painted == []
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists[-1].items == [row for _, row in rows]
        assert lists[-1].selectable is True

    async def test_empty_configured_agents_shows_setup_not_legacy_terminal(self, tmp_path):
        router, transport, _ = make_router(tmp_path, terminal_rows=lambda: [])
        await router.open("Agent Terminal")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists[-1].items[0] == "Add an agent in JOAT on your Mac."
        assert lists[-1].selectable is False


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
        request_id = getattr(lists[-1], "request_id", None)
        router.on_input(InputEvent(
            container_name="capdata", selected_index=2,
            selected_name="BRAINSTORM", request_id=request_id, trace_id="hud",
        ))
        for _ in range(40):
            await asyncio.sleep(0.01)
            if any(isinstance(m, HudList) and m.title == "BRAINSTORM · SAVED" for m in transport.sent):
                break
        assert opened == [], "the first tap opens the saved transcript, not the collaborators"
        # The tap inside the open reader is its action: continue this session.
        router.on_input(InputEvent(
            container_name="capdata", selected_index=0,
            selected_name="BRAINSTORM", request_id=request_id, trace_id="hud",
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


class TestTerminalOpeningLease:
    async def setup_opening(self, tmp_path):  # type: ignore[no-untyped-def]
        entered = asyncio.Event()
        release = asyncio.Event()
        started = []
        painted = []

        async def opener(name, request_id, selection_id, current):
            started.append((name, request_id, selection_id, current))
            entered.set()
            await release.wait()
            if current():
                painted.append(name)

        router, transport, _ = make_router(
            tmp_path, terminal_rows=lambda: [("work", "work"), ("calendar", "calendar")],
            terminal_open=opener,
        )
        await router.open("Agent Terminal", request_id="visit-1")
        return router, transport, entered, release, started, painted

    @pytest.mark.parametrize("leave", ["home", "disconnect", "new-capability", "queued-capability"])
    async def test_held_open_cannot_paint_after_leaving(self, tmp_path, leave):  # type: ignore[no-untyped-def]
        router, _, entered, release, started, painted = await self.setup_opening(tmp_path)
        opening = asyncio.create_task(router._select_capability_row("AGENT TERMINAL", 0, "tap-1", "visit-1"))
        await entered.wait()
        if leave == "home":
            router.on_input(InputEvent(container_name="menu", selected_name="closed"))
        elif leave == "disconnect":
            router.on_disconnect()
        elif leave == "new-capability":
            await router.open("Messages", request_id="visit-2")
        else:
            router.on_input(InputEvent(container_name="capmenu", selected_name="Messages", request_id="visit-2"))
        assert not started[0][3](), "Leaving must retire the opening synchronously"
        release.set()
        await opening
        assert painted == []

    async def test_opening_echoes_exact_visit_and_selection(self, tmp_path):  # type: ignore[no-untyped-def]
        router, _, entered, release, started, painted = await self.setup_opening(tmp_path)
        opening = asyncio.create_task(router._select_capability_row("AGENT TERMINAL", 1, "tap-2", "visit-1"))
        await entered.wait()
        release.set()
        await opening
        assert started[0][:3] == ("calendar", "visit-1", "tap-2")
        assert painted == ["calendar"]

    async def test_new_selection_retires_held_first_selection(self, tmp_path):  # type: ignore[no-untyped-def]
        router, _, entered, release, started, painted = await self.setup_opening(tmp_path)
        first = asyncio.create_task(router._select_capability_row("AGENT TERMINAL", 0, "tap-1", "visit-1"))
        await entered.wait()
        router.on_input(InputEvent(container_name="capdata", selected_name="AGENT TERMINAL", selected_index=1, trace_id="tap-2", request_id="visit-1"))
        assert not started[0][3](), "Second selection must retire the first before queued work runs"
        release.set()
        await first
        for _ in range(3):
            await asyncio.sleep(0)
        assert painted == ["calendar"]

    async def test_home_before_queued_selection_prevents_preparation(self, tmp_path):  # type: ignore[no-untyped-def]
        router, _, _, release, started, _ = await self.setup_opening(tmp_path)
        release.set()
        router.on_input(InputEvent(container_name="capdata", selected_name="AGENT TERMINAL", selected_index=0, trace_id="tap-1", request_id="visit-1"))
        router.on_input(InputEvent(container_name="menu", selected_name="closed"))
        for _ in range(3):
            await asyncio.sleep(0)
        assert started == []

    async def test_old_terminal_home_does_not_retire_newer_selection(self, tmp_path):  # type: ignore[no-untyped-def]
        router, _, entered, release, started, _ = await self.setup_opening(tmp_path)
        first = asyncio.create_task(router._select_capability_row("AGENT TERMINAL", 0, "tap-1", "visit-1"))
        await entered.wait()
        entered.clear()
        second = asyncio.create_task(router._select_capability_row("AGENT TERMINAL", 1, "tap-2", "visit-1"))
        await entered.wait()
        router.on_input(InputEvent(container_name="capability-lifecycle", selected_name="terminal-home", request_id="visit-1", trace_id="tap-1"))
        assert not started[0][3]() and started[1][3]()
        router.on_input(InputEvent(container_name="capability-lifecycle", selected_name="terminal-home", request_id="visit-1", trace_id="tap-2"))
        assert not started[1][3]()
        release.set()
        await asyncio.gather(first, second)
