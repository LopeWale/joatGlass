from __future__ import annotations

import json
import stat

from joat.services.g2_diagnostics import G2DiagnosticStore, TRACE_SCHEMA


async def test_store_writes_a_bounded_content_free_owner_only_receipt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "g2-diagnostics.json"
    store = G2DiagnosticStore(path=path)

    store.record({
        "type": "tile_upload",
        "frame": 2,
        "order": 1,
        "containerId": 8,
        "outcome": "success",
    })
    await store.flush()

    receipt = json.loads(path.read_text())
    assert receipt == {
        "schema": TRACE_SCHEMA,
        "events": [{
            "sequence": 1,
            "recordedAtMs": receipt["events"][0]["recordedAtMs"],
            "type": "tile_upload",
            "frame": 2,
            "order": 1,
            "containerId": 8,
            "outcome": "success",
        }],
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_store_coalesces_one_loop_turn_into_one_background_write(tmp_path) -> None:  # type: ignore[no-untyped-def]
    writes: list[list[dict[str, object]]] = []

    def writer(_path, events) -> None:  # type: ignore[no-untyped-def]
        writes.append(events)

    store = G2DiagnosticStore(path=tmp_path / "g2-diagnostics.json", writer=writer)
    store.record({"type": "gesture", "gesture": "tap", "delivery": "sys"})
    store.record({"type": "gesture", "gesture": "scroll_down", "delivery": "text"})
    await store.flush()

    assert len(writes) == 1
    assert [event["gesture"] for event in writes[0]] == ["tap", "scroll_down"]
