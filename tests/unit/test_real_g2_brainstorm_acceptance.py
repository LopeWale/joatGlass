"""Privacy and contract tests for the physical Brainstorm G2 acceptance receipt."""

from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import websockets

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.pairing import PairingStore
from joat.services.g2_diagnostics import G2DiagnosticStore
from scripts.real_g2_brainstorm_acceptance import (
    RUN_SCHEMA,
    _file_sha256,
    _write_owner_only_json,
    capture,
    summarize_receipt,
)


def _hash(character: str) -> str:
    return character * 64


def _receipt(events: list[dict[str, object]]) -> dict[str, object]:
    stamped: list[dict[str, object]] = []
    for sequence, event in enumerate(events, start=1):
        stamped.append({"sequence": sequence, "recordedAtMs": sequence, **event})
    return {"schema": "joat.g2-diagnostic.v2", "events": stamped}


def _frame(surface: str, digest: str, state: str = "running") -> dict[str, object]:
    return {
        "type": "visual_frame",
        "kind": "brainstorm",
        "surface": surface,
        "state": state,
        "sha256": digest,
    }


def _action(
    action: str,
    surface: str,
    source: str = "right",
    gesture: str = "tap",
) -> dict[str, object]:
    return {
        "type": "brainstorm_action",
        "action": action,
        "surface": surface,
        "gesture": gesture,
        "delivery": "sys",
        "source": source,
    }


def test_pass_requires_real_input_and_distinct_visible_frame_transitions() -> None:
    summary = summarize_receipt(_receipt([
        _frame("brainstorm", _hash("a")),
        _action("show_terminal", "brainstorm"),
        _frame("agent_terminal", _hash("b")),
        _action("show_brainstorm", "agent_terminal", "ring"),
        _frame("brainstorm", _hash("c")),
    ]))

    assert summary == {
        "schema": RUN_SCHEMA,
        "mode": "physical_g2",
        "result": "pass",
        "retainedEventCount": 5,
        "events": [
            {"type": "visual_frame", "kind": "brainstorm", "surface": "brainstorm", "state": "running", "sha256": _hash("a")},
            {"type": "brainstorm_action", "action": "show_terminal", "surface": "brainstorm", "gesture": "tap", "delivery": "sys", "source": "right"},
            {"type": "visual_frame", "kind": "brainstorm", "surface": "agent_terminal", "state": "running", "sha256": _hash("b")},
            {"type": "brainstorm_action", "action": "show_brainstorm", "surface": "agent_terminal", "gesture": "tap", "delivery": "sys", "source": "ring"},
            {"type": "visual_frame", "kind": "brainstorm", "surface": "brainstorm", "state": "running", "sha256": _hash("c")},
        ],
        "wearerVisibleHud": {"bridge": "verified", "lens": "not_recorded"},
        "lensVideoSha256": None,
        "hermesRun": "not_exercised",
        "g2Reconnect": "not_exercised",
    }


def test_summary_never_upgrades_bridge_proof_to_lens_video_proof() -> None:
    summary = summarize_receipt(
        _receipt([
            _frame("brainstorm", _hash("a")),
            _action("show_terminal", "brainstorm"),
            _frame("agent_terminal", _hash("b")),
            _action("show_brainstorm", "agent_terminal"),
            _frame("brainstorm", _hash("c")),
        ]),
        lens_video_sha256=_hash("d"),
    )

    assert summary["result"] == "pass"
    assert summary["wearerVisibleHud"] == {
        "bridge": "verified",
        "lens": "video_digest_recorded",
    }
    assert summary["lensVideoSha256"] == _hash("d")


def test_live_hermes_receipt_requires_left_double_cancel_and_cancelled_remote_run() -> None:
    receipt = _receipt([
        _frame("brainstorm", _hash("a")),
        _action("show_terminal", "brainstorm"),
        _frame("agent_terminal", _hash("b")),
        _action("show_brainstorm", "agent_terminal", "ring"),
        _frame("brainstorm", _hash("c")),
        _action("cancel", "brainstorm", "left", "double_press"),
        {**_frame("brainstorm", _hash("d")), "state": "cancelled"},
    ])

    assert summarize_receipt(
        receipt,
        hermes_run="cancelled",
        g2_reconnect="restored",
        require_cancel=True,
        require_reconnect=True,
    )["result"] == "pass"
    assert summarize_receipt(
        receipt,
        hermes_run="completed",
        g2_reconnect="restored",
        require_cancel=True,
        require_reconnect=True,
    )["result"] == "inconclusive"
    assert summarize_receipt(
        receipt,
        hermes_run="cancelled",
        require_cancel=True,
        require_reconnect=True,
    )["result"] == "inconclusive"


@pytest.mark.parametrize(
    "events, result",
    [
        ([_frame("brainstorm", _hash("a"))], "inconclusive"),
        ([
            _frame("brainstorm", _hash("a")),
            _action("show_terminal", "brainstorm"),
        ], "fail"),
        ([
            _frame("brainstorm", _hash("a")),
            _action("show_terminal", "brainstorm"),
            _frame("agent_terminal", _hash("a")),
        ], "fail"),
    ],
)
def test_summary_fails_closed_when_a_transition_is_missing_or_not_distinct(
    events: list[dict[str, object]], result: str,
) -> None:
    assert summarize_receipt(_receipt(events))["result"] == result


def test_rejects_non_physical_or_content_bearing_brainstorm_trace() -> None:
    with pytest.raises(ValueError, match="physical G2 input"):
        summarize_receipt(_receipt([
            _frame("brainstorm", _hash("a")),
            _action("show_terminal", "brainstorm", "synthetic"),
        ]))

    unsafe = _frame("brainstorm", _hash("a"))
    unsafe["text"] = "private Brainstorm topic"
    with pytest.raises(ValueError, match="unsupported fields"):
        summarize_receipt(_receipt([unsafe]))


def test_rejects_non_hash_lens_evidence_and_does_not_accept_bad_receipts() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        summarize_receipt(_receipt([]), lens_video_sha256="not-a-hash")
    with pytest.raises(ValueError, match="not an opt-in"):
        summarize_receipt({"schema": "wrong", "events": []})


def test_receipt_writer_and_lens_digest_store_only_a_digest(tmp_path: Path) -> None:
    lens_video = tmp_path / "lens.mov"
    lens_video.write_bytes(b"physical lens content")
    receipt = tmp_path / "receipt.json"
    _write_owner_only_json(receipt, {"lensVideoSha256": _file_sha256(lens_video)})

    assert json.loads(receipt.read_text()) == {"lensVideoSha256": _file_sha256(lens_video)}
    assert "physical lens content" not in receipt.read_text()
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


async def _connect_when_ready(url: str) -> Any:
    for _ in range(100):
        try:
            return await websockets.connect(url)
        except OSError:
            await asyncio.sleep(0.01)
    raise AssertionError("acceptance gateway did not start")


@pytest.mark.asyncio
async def test_capture_drives_the_real_gateway_action_sequence_without_content(
    tmp_path: Path, unused_tcp_port: int,
) -> None:
    out = tmp_path / "physical-receipt.json"

    def gateway_factory(store: G2DiagnosticStore) -> HubGatewayTransport:
        return HubGatewayTransport(
            host="127.0.0.1",
            port=unused_tcp_port,
            pairing=PairingStore(path=tmp_path / "pairing.json"),
            # Only this transport test uses loopback.  The CLI's real-G2
            # gateway keeps the physical pairing boundary closed to localhost.
            trust_loopback=True,
            g2_diagnostics=store,
        )

    task = asyncio.create_task(capture(
        out=out,
        port=unused_tcp_port,
        connect_timeout_s=1,
        action_timeout_s=1,
        _gateway_factory=gateway_factory,
    ))
    socket = await _connect_when_ready(f"ws://127.0.0.1:{unused_tcp_port}")
    try:
        assert json.loads(await socket.recv()) == {"kind": "auth.accepted"}
        assert json.loads(await socket.recv()) == {
            "kind": "g2.diagnostic.control", "enabled": True,
        }
        await socket.send(json.dumps({
            "kind": "hello", "sn": "not-retained", "model": "g2", "launchSource": "test",
        }))
        initial = json.loads(await socket.recv())
        assert initial["kind"] == "brainstorm.view"
        assert initial["brainstorm"]["screen"] == "brainstorm"
        await socket.send(json.dumps({"kind": "g2.diagnostic", "event": _frame("brainstorm", _hash("a"))}))
        await socket.send(json.dumps({"kind": "brainstorm.action", "action": "show_terminal"}))
        await socket.send(json.dumps({"kind": "g2.diagnostic", "event": _action("show_terminal", "brainstorm")}))

        terminal = json.loads(await socket.recv())
        assert terminal["kind"] == "brainstorm.view"
        assert terminal["brainstorm"]["screen"] == "agent_terminal"
        await socket.send(json.dumps({"kind": "g2.diagnostic", "event": _frame("agent_terminal", _hash("b"))}))
        await socket.send(json.dumps({"kind": "brainstorm.action", "action": "show_brainstorm"}))
        await socket.send(json.dumps({"kind": "g2.diagnostic", "event": _action("show_brainstorm", "agent_terminal")}))

        returned = json.loads(await socket.recv())
        assert returned["kind"] == "brainstorm.view"
        assert returned["brainstorm"]["screen"] == "brainstorm"
        await socket.send(json.dumps({"kind": "g2.diagnostic", "event": _frame("brainstorm", _hash("c"))}))
        summary = await task
    finally:
        await socket.close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert summary["result"] == "pass"
    assert out.exists()
    output = out.read_text()
    assert "not-retained" not in output
    assert "G2 acceptance" not in output
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_capture_requires_real_action_sequence_for_a_content_free_hermes_run(
    tmp_path: Path, unused_tcp_port: int,
) -> None:
    out = tmp_path / "live-hermes-receipt.json"

    class BlockingHermes:
        def __init__(self) -> None:
            self.prompt: str | None = None
            self.closed = False
            self._never = asyncio.Event()

        async def stream(self, prompt: str) -> AsyncIterator[str]:
            self.prompt = prompt
            await self._never.wait()
            yield "private remote text that must never be retained"

        async def close(self) -> None:
            self.closed = True

    hermes = BlockingHermes()

    @asynccontextmanager
    async def hermes_context() -> AsyncIterator[BlockingHermes]:
        try:
            yield hermes
        finally:
            await hermes.close()

    def gateway_factory(store: G2DiagnosticStore) -> HubGatewayTransport:
        return HubGatewayTransport(
            host="127.0.0.1",
            port=unused_tcp_port,
            pairing=PairingStore(path=tmp_path / "pairing.json"),
            trust_loopback=True,
            g2_diagnostics=store,
        )

    task = asyncio.create_task(capture(
        out=out,
        port=unused_tcp_port,
        connect_timeout_s=1,
        action_timeout_s=1,
        _gateway_factory=gateway_factory,
        _hermes_context=lambda: hermes_context(),
        _require_cancel=True,
        _require_reconnect=True,
    ))
    socket = await _connect_when_ready(f"ws://127.0.0.1:{unused_tcp_port}")
    replacement: Any | None = None
    try:
        assert json.loads(await socket.recv()) == {"kind": "auth.accepted"}
        assert json.loads(await socket.recv()) == {
            "kind": "g2.diagnostic.control", "enabled": True,
        }
        await socket.send(json.dumps({
            "kind": "hello", "sn": "not-retained", "model": "g2", "launchSource": "test",
        }))
        ready = json.loads(await socket.recv())
        assert ready["brainstorm"]["state"] == "ready"
        await socket.send(json.dumps({
            "kind": "g2.diagnostic", "event": _frame("brainstorm", _hash("a"), "ready"),
        }))
        await socket.send(json.dumps({"kind": "brainstorm.action", "action": "begin_topic_capture"}))
        await socket.send(json.dumps({
            "kind": "g2.diagnostic", "event": _action("begin_topic_capture", "brainstorm"),
        }))

        running = json.loads(await socket.recv())
        assert running["brainstorm"]["state"] == "running"
        await socket.send(json.dumps({
            "kind": "g2.diagnostic", "event": _frame("brainstorm", _hash("b")),
        }))
        await socket.send(json.dumps({"kind": "brainstorm.action", "action": "show_terminal"}))
        await socket.send(json.dumps({
            "kind": "g2.diagnostic", "event": _action("show_terminal", "brainstorm"),
        }))

        terminal = json.loads(await socket.recv())
        assert terminal["brainstorm"]["screen"] == "agent_terminal"
        await socket.send(json.dumps({
            "kind": "g2.diagnostic", "event": _frame("agent_terminal", _hash("c")),
        }))
        await socket.send(json.dumps({"kind": "brainstorm.action", "action": "show_brainstorm"}))
        await socket.send(json.dumps({
            "kind": "g2.diagnostic", "event": _action("show_brainstorm", "agent_terminal", "ring"),
        }))

        returned = json.loads(await socket.recv())
        assert returned["brainstorm"]["screen"] == "brainstorm"
        await socket.send(json.dumps({
            "kind": "g2.diagnostic", "event": _frame("brainstorm", _hash("d")),
        }))

        reconnect_prompt = json.loads(await socket.recv())
        assert reconnect_prompt["brainstorm"]["state"] == "running"
        await socket.send(json.dumps({
            "kind": "g2.diagnostic", "event": _frame("brainstorm", _hash("e")),
        }))
        await socket.close()
        replacement = await _connect_when_ready(f"ws://127.0.0.1:{unused_tcp_port}")
        assert json.loads(await replacement.recv()) == {"kind": "auth.accepted"}
        assert json.loads(await replacement.recv()) == {
            "kind": "g2.diagnostic.control", "enabled": True,
        }
        await replacement.send(json.dumps({
            "kind": "hello", "sn": "not-retained", "model": "g2", "launchSource": "reconnect",
        }))
        restored = json.loads(await replacement.recv())
        assert restored["brainstorm"]["state"] == "running"
        await replacement.send(json.dumps({
            "kind": "g2.diagnostic", "event": _frame("brainstorm", _hash("f")),
        }))
        await replacement.send(json.dumps({"kind": "brainstorm.action", "action": "cancel"}))
        await replacement.send(json.dumps({
            "kind": "g2.diagnostic",
            "event": _action("cancel", "brainstorm", "left", "double_press"),
        }))

        cancelled = json.loads(await replacement.recv())
        assert cancelled["brainstorm"]["state"] == "cancelled"
        await replacement.send(json.dumps({
            "kind": "g2.diagnostic", "event": _frame("brainstorm", _hash("a"), "cancelled"),
        }))
        summary = await task
    finally:
        await socket.close()
        if replacement is not None:
            await replacement.close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert summary["result"] == "pass"
    assert summary["hermesRun"] == "cancelled"
    assert summary["g2Reconnect"] == "restored"
    assert hermes.prompt is not None
    assert hermes.closed is True
    output = out.read_text()
    assert "private remote text" not in output
    assert hermes.prompt not in output
    assert "not-retained" not in output
