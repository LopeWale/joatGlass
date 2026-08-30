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
import logging
from pathlib import Path
import sqlite3
import tempfile
import time

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.channels.imessage import IMessageStore, MessageThread
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


_EXPECTED_REPLY_FLOW = (
    ("messages-sidebar", "test-hook-open"),
    ("messages-conversations", "click"),
    ("messages-conversation", "list-select"),
    ("messages-confirmation", None),
    ("messages-result", "click"),
)

_EXPECTED_COMPOSE_FLOW = (
    ("messages-sidebar", "test-hook-open"),
    ("messages-conversations", "click"),
    ("messages-compose", "list-select"),
    ("messages-confirmation", None),
    ("messages-result", "click"),
)


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
        if digest in seen:
            raise MessagesFlowError(f"{name}: frame is not visually distinct")
        seen.add(digest)


def validate_flow(receipts: list[ScreenReceipt]) -> None:
    _validate_receipts(receipts, _EXPECTED_REPLY_FLOW)


def validate_compose_flow(receipts: list[ScreenReceipt]) -> None:
    _validate_receipts(receipts, _EXPECTED_COMPOSE_FLOW)


def build_fixture_store(path: Path) -> IMessageStore:
    """Create two disposable, text-only conversations for simulator proof."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE message (
            handle_id INTEGER, text TEXT, date INTEGER, is_from_me INTEGER
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
    connection.executemany(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        [
            (1, "Are you coming?", apple_now - 180, 0),
            (1, "On my way", apple_now - 120, 1),
            (1, "Great, see you soon.", apple_now - 60, 0),
            (2, "Call me when you arrive.", apple_now, 0),
        ],
    )
    connection.executemany(
        "INSERT INTO chat_message_join VALUES (?, ?)",
        [(1, 1), (2, 1), (3, 1), (4, 2)],
    )
    connection.commit()
    connection.close()
    return IMessageStore(path)


class RecordingSender(MessageSender):
    """The deliberate no-send boundary used only by this isolated trace."""

    def __init__(self) -> None:
        self.sent = asyncio.Event()

    async def send(self, recipient: str, text: str) -> None:
        log.info(
            "stage=recording-sender-reached recipient_fixture=%s chars=%d",
            recipient.startswith("fixture-"),
            len(text),
        )
        self.sent.set()


class ObservedReplies:
    """Observe selection while delegating every reply decision to production."""

    def __init__(self, delegate: MessageReplyController) -> None:
        self._delegate = delegate
        self.selected = asyncio.Event()
        self.composing = asyncio.Event()

    def select(self, thread: MessageThread, trace_id: str) -> None:
        self._delegate.select(thread, trace_id)
        self.selected.set()

    def begin_compose(self, trace_id: str) -> None:
        self._delegate.begin_compose(trace_id)
        self.composing.set()

    def clear_selection(self, reason: str = "explicit") -> None:
        self._delegate.clear_selection(reason)

    async def handle_command(self, text: str) -> str | None:
        return await self._delegate.handle_command(text)


async def run(port: int, flow: str = "reply") -> None:
    connected = asyncio.Event()
    with tempfile.TemporaryDirectory(prefix="joat-messages-simulator-") as root:
        evidence_root = Path(root)
        store = build_fixture_store(evidence_root / "chat.db")
        gateway = HubGatewayTransport(host="127.0.0.1", port=port)
        gateway.on_hello(lambda _hello: connected.set())
        hud = HudWriter(gateway)
        sender = RecordingSender()
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
            selected = replies.composing if flow == "compose" else replies.selected
            log.info("stage=connected flow=%s action=select-Messages-row", flow)
            await asyncio.wait_for(selected.wait(), timeout=90)
            # Retain the dedicated conversation frame before the confirmation
            # replaces it. This delay changes no routing or reply semantics.
            await asyncio.sleep(2)
            log.info("stage=%s-selected action=voice-draft-injected", flow)
            command = (
                "message Simulator Contact saying Simulator trace only. No message will be sent."
                if flow == "compose"
                else "reply saying Simulator trace only. No message will be sent."
            )
            result = await asyncio.wait_for(
                replies.handle_command(command),
                timeout=90,
            )
            if result is None:
                raise MessagesFlowError("production reply command was not recognized")
            await hud.assistant(result)
            await hud.settle_assistant(10_000)
            if not sender.sent.is_set():
                raise MessagesFlowError("confirmation did not reach recording sender")
            log.info("stage=result-visible result_chars=%d", len(result))
            await asyncio.sleep(12)
        finally:
            await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--flow", choices=("reply", "compose"), default="reply")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(run(args.port, args.flow))


if __name__ == "__main__":
    main()
