#!/usr/bin/env python3
"""Exercise the opt-in real-G2 diagnostic path through the official simulator.

This launches the ordinary HubGatewayTransport with a temporary redacted
receipt store, then asks the production plugin to show one no-op confirmation.
Pressing simulator Click produces a page transition, a classified gesture, and
the serial four-tile upload receipt. No message, action, transcript, or device
identifier is supplied to the trace.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.pairing import PairingStore
from joat.services.g2_diagnostics import G2DiagnosticStore

log = logging.getLogger("joat.simulator_g2_diagnostic_trace")


async def run(port: int, out: Path, timeout_s: float) -> None:
    connected = asyncio.Event()
    gateway = HubGatewayTransport(
        host="127.0.0.1",
        port=port,
        pairing=PairingStore(path=out.with_name("simulator-g2-diagnostic-pairing.json")),
        g2_diagnostics=G2DiagnosticStore(path=out),
    )
    gateway.on_hello(lambda _hello: connected.set())
    await gateway.start()
    try:
        log.info("waiting for the ordinary simulator plugin on port=%d", port)
        await asyncio.wait_for(connected.wait(), timeout_s)
        accepted = await gateway.request_confirm(
            "simulator-g2-diagnostic",
            "Simulator diagnostic capture. Click confirms no external action.",
            timeout_s=timeout_s,
        )
        log.info("scenario complete: accepted=%s receipt=%s", accepted, out)
        # The confirmation callback sends its verdict before the presenter has
        # completed the final four-tile restore. Keep the gateway alive long
        # enough for that ordinary production repaint and transition receipt.
        await asyncio.sleep(1)
    finally:
        await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(run(args.port, args.out, args.timeout))


if __name__ == "__main__":
    main()
