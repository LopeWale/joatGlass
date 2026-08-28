#!/usr/bin/env python3
"""Drive the production reply gate through a real gateway and simulator.

This is deliberately safe: it uses the production MessageReplyController and
HubGatewayTransport, but replaces the final Messages.app boundary with a
recording sender. The simulator therefore proves the visible confirmation and
uplink verdict without sending an external message.

Run a plugin dev server pointed at this port, then launch the ordinary Even Hub
simulator URL and press Click once the confirmation is fully visible.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.imessage import MessageThread
from joat.core.interfaces import MessageSender
from joat.core.protocol import AudioControl
from joat.services.message_reply import MessageReplyController

log = logging.getLogger("joat.simulator_reply_trace")


class RecordingSender(MessageSender):
    """Evidence boundary: record shape, never call Messages.app."""

    def __init__(self) -> None:
        self.sent = 0

    async def send(self, recipient: str, text: str) -> None:
        self.sent += 1
        log.info("action stage=recording-sender-reached chars=%d", len(text))


async def run(port: int) -> None:
    connected = asyncio.Event()
    gateway = HubGatewayTransport(host="127.0.0.1", port=port)
    gateway.on_hello(lambda _hello: connected.set())
    sender = RecordingSender()
    replies = MessageReplyController(sender, gateway.request_confirm)
    replies.select(
        MessageThread(
            sender="SIMULATOR TEST (NO SEND)",
            handle="simulator-fixture",
            text="This is an isolated reply trace.",
            at=0.0,
        ),
        trace_id="hud-simulator-reply",
    )

    await gateway.start()
    try:
        log.info("waiting for ordinary simulator plugin on port=%d", port)
        await asyncio.wait_for(connected.wait(), 30.0)
        # Keep the automation console about the action under test instead of
        # filling it with live microphone PCM while we inspect the frame.
        await gateway.send(AudioControl(open=False))
        # The plugin announces before its final boot paints. Let that settle so
        # the screenshot is the real decision surface, not an in-flight frame.
        await asyncio.sleep(2.0)
        await replies.handle_command(
            "reply saying Trace only. No message will be sent."
        )
        outcome = "sent" if sender.sent == 1 else "cancelled"
        log.info(
            "action stage=scenario-complete sends=%d outcome=%s",
            sender.sent,
            outcome,
        )
        await asyncio.sleep(1.0)
    finally:
        await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18790)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(run(args.port))


if __name__ == "__main__":
    main()
