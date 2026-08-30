#!/usr/bin/env python3
"""Exercise the real Messages menu/router/reply or compose path in the simulator.

The simulator test hook opens the ordinary sidebar; the wearer then taps
Messages, selects a conversation or New message, and accepts the confirmation.
The Hub plugin, gateway, CapabilityRouter, IMessageStore and
MessageReplyController are production implementations. The database and final
sender are isolated fixtures, so this is simulator proof and cannot send a
real text message or count as physical-G2 proof.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Literal, cast
import urllib.error
import urllib.request

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.channels.imessage import IMessageStore, MessageThread
from joat.channels.message_sender import MessageSendError
from joat.core.interfaces import MessageSender
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.message_reply import MessageReplyController
from joat.services.status import StatusPublisher

log = logging.getLogger("joat.simulator_messages_flow")


class MessagesFlowError(RuntimeError):
    """The retained simulator frames do not prove the causal wearer flow."""


@dataclass(frozen=True, slots=True)
class ScreenReceipt:
    name: str
    simulator_input: str | None
    framebuffer_sha256: str


ReplyOutcome = Literal["accepted", "cancelled", "timed_out", "uncertain", "failed"]


@dataclass(frozen=True, slots=True)
class ReplyOutcomeExpectation:
    """The result must match the real confirmation and sender boundary.

    The simulator still supplies the gesture; this contract tells the operator
    whether that gesture is a click, swipe, or deliberate silence, and makes
    the no-send fixture fail if the production controller labels the outcome
    more confidently than the sender could prove.
    """

    simulator_gesture: Literal["click", "swipe"] | None
    simulator_control: Literal["click", "down"] | None
    wearer_label: Literal["TAP · Confirm", "SWIPE · Cancel"] | None
    sender_called: bool
    result_template: str
    sender_error: MessageSendError | None = None

    @property
    def result_text(self) -> str:
        """Legacy fixed-recipient contract used by the unit-level matrix."""
        return self.result_for("Dele")

    def result_for(self, recipient: str) -> str:
        """Expected production wording for the thread actually selected."""
        return self.result_template.format(recipient=recipient)


def reply_outcome_expectation(outcome: str) -> ReplyOutcomeExpectation:
    """Return the wearer's action and exact expected reply result for one run."""
    expectations: dict[ReplyOutcome, ReplyOutcomeExpectation] = {
        "accepted": ReplyOutcomeExpectation(
            simulator_gesture="click",
            simulator_control="click",
            wearer_label="TAP · Confirm",
            sender_called=True,
            result_template="Submitted to Messages for {recipient}; delivery unverified.",
        ),
        "cancelled": ReplyOutcomeExpectation(
            simulator_gesture="swipe",
            simulator_control="down",
            wearer_label="SWIPE · Cancel",
            sender_called=False,
            result_template="Reply cancelled.",
        ),
        "timed_out": ReplyOutcomeExpectation(
            simulator_gesture=None,
            simulator_control=None,
            wearer_label=None,
            sender_called=False,
            result_template="Reply cancelled.",
        ),
        "uncertain": ReplyOutcomeExpectation(
            simulator_gesture="click",
            simulator_control="click",
            wearer_label="TAP · Confirm",
            sender_called=True,
            result_template=(
                "Messages may have submitted the reply to {recipient}. "
                "Check Messages on your Mac before trying again."
            ),
            sender_error=MessageSendError(
                "simulated uncertain Messages outcome",
                possible_external_commit=True,
            ),
        ),
        "failed": ReplyOutcomeExpectation(
            simulator_gesture="click",
            simulator_control="click",
            wearer_label="TAP · Confirm",
            sender_called=True,
            result_template="Could not send to {recipient}. Check Messages on your Mac.",
            sender_error=MessageSendError(
                "simulated unavailable Messages",
                possible_external_commit=False,
            ),
        ),
    }
    try:
        return expectations[cast(ReplyOutcome, outcome)]
    except KeyError as exc:
        raise ValueError(f"unsupported reply outcome: {outcome}") from exc


def _http_bytes(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
) -> bytes:
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return cast(bytes, response.read())


async def _clear_simulator_console(automation_base: str) -> None:
    try:
        await asyncio.to_thread(
            _http_bytes,
            f"{automation_base}/api/console",
            method="DELETE",
        )
    except (OSError, urllib.error.URLError) as exc:
        raise MessagesFlowError(
            "official simulator console could not be cleared before the reply confirmation"
        ) from exc


def _write_framebuffer(destination: Path, framebuffer: bytes) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(framebuffer)
    destination.chmod(0o600)
    return hashlib.sha256(framebuffer).hexdigest()


async def _capture_frame(
    automation_base: str,
    evidence_dir: Path,
    name: str,
    simulator_input: str | None,
    *,
    settle_s: float = 0.5,
) -> ScreenReceipt:
    """Retain one settled 576×288 production frame with its causal input."""
    await asyncio.sleep(settle_s)
    try:
        framebuffer = await asyncio.to_thread(
            _http_bytes,
            f"{automation_base}/api/screenshot/glasses",
        )
    except (OSError, urllib.error.URLError) as exc:
        raise MessagesFlowError(
            f"official simulator did not provide the {name!r} Messages frame"
        ) from exc
    digest = _write_framebuffer(evidence_dir / f"{name}.png", framebuffer)
    return ScreenReceipt(name, simulator_input, digest)


async def _wait_for_visible_confirmation(
    automation_base: str,
    evidence_dir: Path,
    *,
    timeout_s: float = 15.0,
) -> ScreenReceipt:
    """Require the production Hub to report its rendered confirmation first."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            console = await asyncio.to_thread(_http_bytes, f"{automation_base}/api/console")
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
            continue
        if b"confirmation-visible" in console:
            return await _capture_frame(
                automation_base,
                evidence_dir,
                "messages-confirmation",
                None,
                settle_s=0,
            )
        await asyncio.sleep(0.1)
    raise MessagesFlowError(
        "official simulator did not report a visible Messages confirmation before timeout"
    )


async def _send_simulator_input(automation_base: str, action: Literal["click", "down"]) -> None:
    try:
        await asyncio.to_thread(
            _http_bytes,
            f"{automation_base}/api/input",
            method="POST",
            body=json.dumps({"action": action}).encode("utf-8"),
        )
    except (OSError, urllib.error.URLError) as exc:
        raise MessagesFlowError(
            f"official simulator did not accept the {action!r} Messages control"
        ) from exc


async def _wait_for_sidebar_frame(
    automation_base: str,
    evidence_dir: Path,
    *,
    timeout_s: float = 15.0,
) -> ScreenReceipt:
    """Require the production test hook to report the ordinary sidebar first."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            console = await asyncio.to_thread(_http_bytes, f"{automation_base}/api/console")
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
            continue
        if b"live menu acceptance opened=true" in console:
            return await _capture_frame(
                automation_base,
                evidence_dir,
                "messages-sidebar",
                "test-hook-open",
            )
        await asyncio.sleep(0.1)
    raise MessagesFlowError("official simulator did not render the Messages sidebar")


_EXPECTED_REPLY_FLOW = (
    ("messages-sidebar", "test-hook-open"),
    ("messages-conversations", "click"),
    ("messages-conversation", "list-select"),
    ("messages-confirmation", None),
    ("messages-result", "click"),
    ("messages-returned", "click"),
)

_EXPECTED_COMPOSE_FLOW = (
    ("messages-sidebar", "test-hook-open"),
    ("messages-conversations", "click"),
    ("messages-compose", "list-select"),
    ("messages-confirmation", None),
    ("messages-result", "click"),
    ("messages-returned", "click"),
)

_EXPECTED_HISTORY_FLOW = (
    ("messages-sidebar", "test-hook-open"),
    ("messages-conversations", "click"),
    ("messages-latest", "list-select"),
    ("messages-older", "scroll-down"),
    ("messages-latest-returned", "scroll-up"),
)


def required_evidence_frames(flow: str) -> tuple[str, ...]:
    """Return every named frame the no-send simulator run must retain."""
    expected = {
        "reply": _EXPECTED_REPLY_FLOW,
        "compose": _EXPECTED_COMPOSE_FLOW,
        "history": _EXPECTED_HISTORY_FLOW,
    }.get(flow)
    if expected is None:
        raise ValueError(f"unsupported Messages evidence flow: {flow}")
    return tuple(name for name, _action in expected)


def _write_evidence_receipt(
    evidence_dir: Path,
    *,
    flow: Literal["reply", "compose", "history"],
    outcome: ReplyOutcome,
    expectation: ReplyOutcomeExpectation,
    sender_called: bool,
    frames: list[ScreenReceipt],
) -> None:
    """Persist only causal frame metadata, never fixture-message content."""
    expected_names = required_evidence_frames(flow)
    actual_names = tuple(frame.name for frame in frames)
    if actual_names != expected_names:
        raise MessagesFlowError(
            f"evidence frames do not match {flow!r} route: {actual_names!r}"
        )
    digests = {frame.name: frame.framebuffer_sha256 for frame in frames}
    receipt = {
        "proof": "official_simulator",
        "flow": flow,
        "outcome": outcome,
        "wearer_label": expectation.wearer_label,
        "simulator_control": expectation.simulator_control,
        "sender_called": sender_called,
        "confirmation_frame_sha256": digests.get("messages-confirmation"),
        "result_frame_sha256": digests.get("messages-result"),
        "frames": [
            {
                "name": frame.name,
                "simulator_input": frame.simulator_input,
                "framebuffer_sha256": frame.framebuffer_sha256,
            }
            for frame in frames
        ],
        "physical_g2_proof": False,
        "messages_app_send": False,
    }
    destination = evidence_dir / "receipt.json"
    destination.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    destination.chmod(0o600)


def _validate_receipts(
    receipts: list[ScreenReceipt],
    expected: tuple[tuple[str, str | None], ...],
) -> None:
    if len(receipts) != len(expected):
        raise MessagesFlowError(
            f"expected {len(expected)} receipts, got {len(receipts)}"
        )
    seen: set[str] = set()
    for receipt, (name, action) in zip(receipts, expected):
        if receipt.name != name or receipt.simulator_input != action:
            raise MessagesFlowError(
                f"{name}: expected input {action!r}, got "
                f"{receipt.name}/{receipt.simulator_input!r}"
            )
        digest = receipt.framebuffer_sha256
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise MessagesFlowError(f"{name}: invalid framebuffer digest")
        # Dismissing the transient result overlay can legitimately reveal the
        # exact same conversation pixels that preceded confirmation. Every
        # other state must be visually distinct, so a stale/static capture
        # cannot satisfy the causal route.
        if digest in seen and name != "messages-returned":
            raise MessagesFlowError(f"{name}: frame is not visually distinct")
        seen.add(digest)


def validate_flow(receipts: list[ScreenReceipt]) -> None:
    _validate_receipts(receipts, _EXPECTED_REPLY_FLOW)


def validate_compose_flow(receipts: list[ScreenReceipt]) -> None:
    _validate_receipts(receipts, _EXPECTED_COMPOSE_FLOW)


def validate_history_flow(receipts: list[ScreenReceipt]) -> None:
    _validate_receipts(receipts, _EXPECTED_HISTORY_FLOW)


def build_fixture_store(path: Path) -> IMessageStore:
    """Create disposable conversations with a scrollable, metadata-rich thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE message (
            handle_id INTEGER, text TEXT, date INTEGER, is_from_me INTEGER,
            is_read INTEGER, is_sent INTEGER, is_delivered INTEGER,
            date_delivered INTEGER, date_read INTEGER,
            cache_has_attachments INTEGER
        );
        CREATE TABLE handle (id TEXT);
        CREATE TABLE chat (display_name TEXT);
        CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);

        INSERT INTO handle VALUES ('fixture-dele');
        INSERT INTO handle VALUES ('fixture-adaeze');
        INSERT INTO chat VALUES ('Dele');
        INSERT INTO chat VALUES ('Adaeze');
        INSERT INTO chat_handle_join VALUES (1, 1);
        INSERT INTO chat_handle_join VALUES (2, 2);

    """)
    apple_now = int(time.time() - 978_307_200)
    dele_history = [
        (1, f"Earlier fixture message {index}", apple_now - (660 - index * 60),
         0, 1, 0, 0, 0, 0, 0)
        for index in range(1, 9)
    ]
    connection.executemany(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            *dele_history,
            (1, "Read reply", apple_now - 60, 1, 1, 1, 1,
             apple_now - 60, apple_now - 30, 0),
            (1, None, apple_now, 0, 0, 0, 0, 0, 0, 1),
            (2, "Call me when you arrive.", apple_now + 1, 0, 1, 0, 0, 0, 0, 0),
        ],
    )
    connection.executemany(
        "INSERT INTO chat_message_join VALUES (?, ?)",
        [*((row_id, 1) for row_id in range(1, 11)), (11, 2)],
    )
    connection.commit()
    connection.close()
    return IMessageStore(path)


class RecordingSender(MessageSender):
    """The deliberate no-send boundary used only by this isolated trace."""

    def __init__(self, error: MessageSendError | None = None) -> None:
        self.called = asyncio.Event()
        self.sent = asyncio.Event()
        self._error = error

    async def send(self, recipient: str, text: str) -> None:
        log.info(
            "stage=recording-sender-reached recipient_fixture=%s chars=%d error=%s",
            recipient.startswith("fixture-"),
            len(text),
            type(self._error).__name__ if self._error is not None else "none",
        )
        self.called.set()
        if self._error is not None:
            raise self._error
        self.sent.set()


class ObservedReplies:
    """Observe selection while delegating every reply decision to production."""

    def __init__(self, delegate: MessageReplyController) -> None:
        self._delegate = delegate
        self.selected = asyncio.Event()
        self.selected_thread: MessageThread | None = None
        self.composing = asyncio.Event()

    def select(self, thread: MessageThread, trace_id: str) -> None:
        self._delegate.select(thread, trace_id)
        self.selected_thread = thread
        self.selected.set()

    def begin_compose(self, trace_id: str) -> None:
        self._delegate.begin_compose(trace_id)
        self.composing.set()

    def clear_selection(self, reason: str = "explicit") -> None:
        self._delegate.clear_selection(reason)

    async def handle_command(self, text: str) -> str | None:
        return await self._delegate.handle_command(text)


async def _wait_for_messages_picker(router: CapabilityRouter) -> None:
    """Observe the Mac-produced picker before recording its rendered frame."""
    while router._active_capability != "MESSAGES" or not router._message_rows:
        await asyncio.sleep(0.05)


async def run(
    port: int,
    flow: Literal["reply", "compose", "history"] = "reply",
    outcome: ReplyOutcome = "accepted",
    *,
    automation_base: str | None = None,
    evidence_dir: Path | None = None,
) -> None:
    if flow != "reply" and outcome != "accepted":
        raise MessagesFlowError(
            "only reply flow accepts a non-success outcome; compose/history stay no-send fixtures"
        )
    if (automation_base is None) != (evidence_dir is None):
        raise MessagesFlowError(
            "official simulator proof requires both automation_base and evidence_dir"
        )
    expectation = reply_outcome_expectation(outcome)
    connected = asyncio.Event()
    with tempfile.TemporaryDirectory(prefix="joat-messages-simulator-") as root:
        evidence_root = Path(root)
        store = build_fixture_store(evidence_root / "chat.db")
        gateway = HubGatewayTransport(host="127.0.0.1", port=port)
        gateway.on_hello(lambda _hello: connected.set())
        hud = HudWriter(gateway)
        sender = RecordingSender(expectation.sender_error)
        replies = ObservedReplies(MessageReplyController(
            sender,
            gateway.request_confirmation,
            contact_search=lambda query: [
                ("Simulator Contact", "fixture-recipient")
            ] if query.casefold() == "simulator contact" else [],
        ))
        router = CapabilityRouter(
            hud=hud,
            transport=gateway,
            status=StatusPublisher(evidence_root / "status.json"),
            messages=store,
            cards=CardLog(),
            replies=replies,  # type: ignore[arg-type]
        )
        gateway.on_input(router.on_input)

        await gateway.start()
        try:
            log.info("stage=waiting-for-official-simulator port=%d", port)
            await asyncio.wait_for(connected.wait(), timeout=60)
            frames: list[ScreenReceipt] = []
            if automation_base is not None:
                assert evidence_dir is not None
                frames.append(await _wait_for_sidebar_frame(automation_base, evidence_dir))
            selected = replies.composing if flow == "compose" else replies.selected
            log.info("stage=connected flow=%s action=select-Messages-row", flow)
            if automation_base is not None:
                assert evidence_dir is not None
                await asyncio.wait_for(_wait_for_messages_picker(router), timeout=90)
                frames.append(await _capture_frame(
                    automation_base,
                    evidence_dir,
                    "messages-conversations",
                    "click",
                ))
            await asyncio.wait_for(selected.wait(), timeout=90)
            if automation_base is not None:
                assert evidence_dir is not None
                selected_frame = (
                    "messages-compose" if flow == "compose"
                    else "messages-latest" if flow == "history"
                    else "messages-conversation"
                )
                frames.append(await _capture_frame(
                    automation_base,
                    evidence_dir,
                    selected_frame,
                    "list-select",
                ))
            if flow == "history":
                # Select the fixture Dele conversation, then use official
                # simulator scroll input. No voice command or confirmation is
                # injected: this trace is deliberately a no-send page-routing
                # proof, not a Messages.app submission test.
                await asyncio.wait_for(_wait_for_history_page(router, 1), timeout=90)
                log.info("stage=older-page-visible action=scroll-down")
                if automation_base is not None:
                    assert evidence_dir is not None
                    frames.append(await _capture_frame(
                        automation_base,
                        evidence_dir,
                        "messages-older",
                        "scroll-down",
                    ))
                await asyncio.wait_for(_wait_for_history_page(router, 0), timeout=90)
                if sender.sent.is_set():
                    raise MessagesFlowError("history-only trace reached sender")
                log.info("stage=latest-page-returned action=scroll-up no_send=true")
                if automation_base is not None:
                    assert evidence_dir is not None
                    frames.append(await _capture_frame(
                        automation_base,
                        evidence_dir,
                        "messages-latest-returned",
                        "scroll-up",
                    ))
                    validate_history_flow(frames)
                    _write_evidence_receipt(
                        evidence_dir,
                        flow=flow,
                        outcome=outcome,
                        expectation=expectation,
                        sender_called=False,
                        frames=frames,
                    )
                await asyncio.sleep(1)
                return
            log.info("stage=%s-selected action=voice-draft-injected", flow)
            command = (
                "message Simulator Contact saying Simulator trace only. No message will be sent."
                if flow == "compose"
                else "reply saying Simulator trace only. No message will be sent."
            )
            if automation_base is not None:
                await _clear_simulator_console(automation_base)
            command_task = asyncio.create_task(replies.handle_command(command))
            confirmation_frame: ScreenReceipt | None = None
            if automation_base is not None:
                assert evidence_dir is not None
                confirmation_frame = await _wait_for_visible_confirmation(
                    automation_base,
                    evidence_dir,
                )
                frames.append(confirmation_frame)
                log.info(
                    "stage=confirmation-visible outcome=%s frame_sha256=%s",
                    outcome,
                    confirmation_frame.framebuffer_sha256,
                )
                if expectation.simulator_control is not None:
                    await _send_simulator_input(automation_base, expectation.simulator_control)
                    log.info(
                        "stage=confirmation-control action=%s label=%s",
                        expectation.simulator_control,
                        expectation.wearer_label,
                    )
            result = await asyncio.wait_for(command_task, timeout=90)
            if result is None:
                raise MessagesFlowError("production reply command was not recognized")
            if flow == "compose":
                expected_result = expectation.result_for("Simulator Contact")
            else:
                selected_thread = replies.selected_thread
                if selected_thread is None:
                    raise MessagesFlowError("reply flow reached a command without a selected thread")
                expected_result = expectation.result_for(selected_thread.sender)
            if result != expected_result:
                raise MessagesFlowError(
                    f"{outcome}: expected {expected_result!r}, got {result!r}"
                )
            await hud.assistant(result)
            await hud.settle_assistant(10_000)
            if sender.called.is_set() is not expectation.sender_called:
                raise MessagesFlowError(
                    f"{outcome}: sender_called={sender.called.is_set()} did not match "
                    f"expected {expectation.sender_called}"
                )
            if outcome == "accepted" and not sender.sent.is_set():
                raise MessagesFlowError("accepted confirmation did not reach recording sender")
            if outcome != "accepted" and sender.sent.is_set():
                raise MessagesFlowError(f"{outcome}: fixture sender reported a successful submission")
            if automation_base is not None:
                assert evidence_dir is not None
                result_frame = await _capture_frame(
                    automation_base,
                    evidence_dir,
                    "messages-result",
                    expectation.simulator_control,
                )
                frames.append(result_frame)
                await _send_simulator_input(automation_base, "click")
                log.info("stage=result-dismissed action=click")
                frames.append(await _capture_frame(
                    automation_base,
                    evidence_dir,
                    "messages-returned",
                    "click",
                ))
                if flow == "reply":
                    validate_flow(frames)
                else:
                    validate_compose_flow(frames)
                _write_evidence_receipt(
                    evidence_dir,
                    flow=flow,
                    outcome=outcome,
                    expectation=expectation,
                    sender_called=sender.called.is_set(),
                    frames=frames,
                )
            log.info(
                "stage=result-visible outcome=%s gesture=%s sender_called=%s result_chars=%d",
                outcome,
                expectation.simulator_gesture or "timeout",
                sender.called.is_set(),
                len(result),
            )
            await asyncio.sleep(1)
        finally:
            await gateway.stop()


async def _wait_for_history_page(router: CapabilityRouter, index: int) -> None:
    while router._message_history is None or router._message_history.index != index:
        await asyncio.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--flow", choices=("reply", "compose", "history"), default="reply")
    parser.add_argument(
        "--outcome",
        choices=("accepted", "cancelled", "timed_out", "uncertain", "failed"),
        default="accepted",
        help=(
            "reply outcome: TAP · Confirm for accepted/uncertain/failed, SWIPE · Cancel "
            "for cancelled, or wait for timed_out; only reply supports a non-success outcome"
        ),
    )
    parser.add_argument(
        "--automation-base",
        help=(
            "official Even Hub simulator automation origin; when set, waits for the "
            "rendered confirmation and injects the documented outcome control"
        ),
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="required with --automation-base; receives confirmation/result frames and receipt",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(
        run(
            args.port,
            args.flow,
            args.outcome,
            automation_base=args.automation_base,
            evidence_dir=args.evidence_dir,
        )
    )


if __name__ == "__main__":
    main()
