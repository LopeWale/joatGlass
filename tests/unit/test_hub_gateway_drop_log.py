"""Downlinks with no glasses connected are counted, not logged one by one."""

from __future__ import annotations

import logging

import pytest

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.protocol import HudAmbient, HudText


@pytest.mark.asyncio
async def test_drops_while_disconnected_log_once_per_spell(caplog: pytest.LogCaptureFixture) -> None:
    # Every frame used to log its own warning: ~1,500 identical lines in
    # three days while the glasses were away, burying everything else.
    gateway = HubGatewayTransport(host="127.0.0.1", port=0)
    with caplog.at_level(logging.WARNING, logger="joat.ble.hub_gateway"):
        for _ in range(25):
            await gateway.send(HudText(text="x"))
            await gateway.send(HudAmbient(temperature="18°", condition="Cloudy", unread=0))
    drops = [record for record in caplog.records if "no plugin connected" in record.getMessage()]
    assert len(drops) == 1
    assert "further drops are counted" in drops[0].getMessage()
