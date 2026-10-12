"""The plugin's downlink-watchdog loop is named in brain.log, once per streak.

On 2026-10-08/09 the real G2 reconnected 2,352 times, each connection
living ~15 s and closing with code 1005, because one Mac->plugin message
hung on the glasses and MacLink's 15 s downlink watchdog dropped the link.
brain.log recorded every drop and explained none of them; a spoken Messages
reply "did nothing" for two days. These tests pin the explanation.
"""

from __future__ import annotations

import logging

import pytest

from joat.ble.hub_gateway import (
    PLUGIN_DOWNLINK_WATCHDOG_S,
    PLUGIN_WATCHDOG_LOOP_THRESHOLD,
    HubGatewayTransport,
)


def _gateway() -> HubGatewayTransport:
    return HubGatewayTransport(host="127.0.0.1", port=0)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage() for record in caplog.records
        if record.levelno == logging.WARNING and "downlink watchdog" in record.getMessage()
    ]


def test_three_watchdog_shaped_drops_in_a_row_are_named_once(caplog: pytest.LogCaptureFixture) -> None:
    gateway = _gateway()
    with caplog.at_level(logging.INFO, logger="joat.ble.hub_gateway"):
        # The 2026-10-08 shape: 15.1 s, 15.2 s, 15.1 s, code 1005, no reason.
        for lifetime in (15.1, 15.2, 15.1, 15.3, 15.1):
            gateway._note_link_end(lifetime, 1005)
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert f"{PLUGIN_WATCHDOG_LOOP_THRESHOLD} times in a row" in warnings[0]
    assert f"about {PLUGIN_DOWNLINK_WATCHDOG_S:.0f} s" in warnings[0]
    assert "spoken reply" in warnings[0]
    assert "scripts/reply_trace.py" in warnings[0]


@pytest.mark.parametrize(
    "lifetimes, codes",
    [
        # Two drops is a phone backgrounding its WebView twice, not the loop.
        ((15.1, 15.2), (1005, 1005)),
        # Long, healthy links that happen to end without a close frame.
        ((1771.8, 3174.6, 795.7), (1005, 1005, 1006)),
        # A clean close or a suspension is the peer's own word, not a hang.
        ((15.1, 15.1, 15.1), (1000, 1001, 1000)),
        # A dial that never settled is not a watchdog drop.
        ((0.02, 0.02, 0.02), (1005, 1005, 1005)),
        # The 15 s beat broken by one long link never reaches three in a row.
        ((15.1, 15.2, 1721.6, 15.1, 15.2), (1005, 1005, 1001, 1005, 1005)),
    ],
)
def test_other_disconnect_shapes_are_not_called_a_watchdog_loop(
    caplog: pytest.LogCaptureFixture, lifetimes: tuple[float, ...], codes: tuple[int, ...],
) -> None:
    gateway = _gateway()
    with caplog.at_level(logging.INFO, logger="joat.ble.hub_gateway"):
        for lifetime, code in zip(lifetimes, codes, strict=True):
            gateway._note_link_end(lifetime, code)
    assert _warnings(caplog) == []


def test_a_held_link_closes_the_streak_and_a_new_loop_is_named_again(caplog: pytest.LogCaptureFixture) -> None:
    gateway = _gateway()
    with caplog.at_level(logging.INFO, logger="joat.ble.hub_gateway"):
        for _ in range(3):
            gateway._note_link_end(15.1, 1005)
        gateway._note_link_end(1495.1, 1006)  # the link held: the fix landed
        for _ in range(3):
            gateway._note_link_end(14.9, 1005)  # ... and later it is back
    messages = [record.getMessage() for record in caplog.records]
    assert len(_warnings(caplog)) == 2
    held = [m for m in messages if "held past the watchdog" in m]
    assert held == ["plugin link held past the watchdog after 3 watchdog-shaped drops"]


def test_a_missing_close_code_or_lifetime_is_tolerated(caplog: pytest.LogCaptureFixture) -> None:
    gateway = _gateway()
    with caplog.at_level(logging.INFO, logger="joat.ble.hub_gateway"):
        gateway._note_link_end(15.1, None)
        gateway._note_link_end(float("nan"), 1005)
        gateway._note_link_end(15.1, "1005")
    assert _warnings(caplog) == []
