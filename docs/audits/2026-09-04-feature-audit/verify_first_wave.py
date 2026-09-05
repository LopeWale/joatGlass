"""Replay identical isolated Mac/Hub journeys before and after the first repairs.

Entry is injected through the Mac controller. Subsequent taps and swipes are
official simulator input through EventRouter and WebSocket. The simulator's
right-only double-click source is explicitly normalized to left in this
disposable fixture to exercise contextual Back; this is not hardware proof.
Capture and message data are synthetic; the host microphone is disabled only
in the temporary fixture. No installed package or live Mac service is touched.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parents[3]
SIM = Path.home() / ".local/lib/node_modules/@evenrealities/evenhub-simulator/node_modules/@evenrealities/sim-darwin-arm64/bin/evenhub-simulator"
BASE = Path(__file__).resolve().parent / "evidence/remediation"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline", action="store_true")
parser.add_argument("--probe-switch", action="store_true", help="Bounded entry/transport diagnostic only")
parser.add_argument("--probe-save", action="store_true", help="Include save/overlay in the short switch diagnostic")
parser.add_argument("--journey", choices=("all", "quicknotes", "messages", "news", "note-reader", "exact-readers"), default="all")
parser.add_argument("--run-label", default="simulator")
parser.add_argument("--manual-review", action="store_true",
                    help="Keep the isolated simulator open for interactive visual inspection")
parser.add_argument("--message-first-tap", action="store_true",
                    help="Require immediate conversation tap to open the first message, without a preparatory swipe")
parser.add_argument("--capture-render-input", action="store_true",
                    help="Observe the actual synthetic HUD canvas before grayscale conversion; output is unchanged")
parser.add_argument("--raw-gray8-probe", action="store_true",
                    help="Diagnostic only: send the same raster as raw Gray8 in the disposable Hub copy")
parser.add_argument("--packed-gray4-probe", action="store_true",
                    help="Diagnostic only: test row-major packed Gray4, high nibble first")
parser.add_argument("--luma-probe", action="store_true",
                    help="Diagnostic only: use weighted sRGB grayscale in the disposable renderer")
parser.add_argument("--exact-feature", choices=("message", "news", "note"), action="append",
                    help="Retry only named exact-reader families; omitted means all three")
args = parser.parse_args()
OUT = BASE / (args.run_label + ("-before" if args.baseline else "-after"))
if (args.journey == "exact-readers" or args.manual_review) and OUT.exists() and any(OUT.iterdir()):
    raise SystemExit("exact-readers requires a fresh --run-label; existing evidence is retained")
OUT.mkdir(parents=True, exist_ok=True)
scratch = tempfile.TemporaryDirectory(prefix="joat-repair-journey-")
TEMP = Path(scratch.name)
mac_root = TEMP / "mac"
shutil.copytree(REPO / "apps/mac/joat", mac_root / "joat", ignore=shutil.ignore_patterns("__pycache__"))
if args.baseline:
    for relative in ("services/capabilities.py", "channels/imessage.py"):
        (mac_root / "joat" / relative).write_bytes(subprocess.check_output(
            ["git", "show", f"8386bf5f4a69239c7f7ee84a709cf47701ea242c:apps/mac/joat/{relative}"], cwd=REPO))
sys.path[:0] = [str(mac_root), str(REPO / "scripts")]

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.protocol import HudList, HudPanel
from joat.persistence.notes import NoteSource, NoteStore
from joat.persistence.store import DurableStore
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.panels import PanelPublisher, PanelSnapshot, Reader, paginate
from joat.services.status import StatusPublisher
from simulator_brainstorm_action_flow_acceptance import _prepare_simulator_plugin_root

spec = importlib.util.spec_from_file_location("repair_fixture_helpers", REPO / "tests/unit/test_capabilities.py")
helpers = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helpers
spec.loader.exec_module(helpers)


def http(path: str, payload: dict | None = None) -> bytes:
    request = urllib.request.Request("http://127.0.0.1:9795" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.read()


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


class ObservedGateway(HubGatewayTransport):
    def __init__(self):
        super().__init__(host="127.0.0.1", port=18896)
        self.frames = []

    async def send(self, message):
        await super().send(message)
        if isinstance(message, (HudList, HudPanel)):
            self.frames.append(message.to_wire())


class FixtureNews:
    tab = "news"

    def __init__(self):
        self.opened = []

    def snapshot(self):
        return PanelSnapshot(rows=tuple(f"Story {i + 1:02d} · Synthetic publisher" for i in range(12)))

    async def refresh(self):
        pass

    async def open(self, index):
        self.opened.append(index)
        return Reader(tab="news", title="NEWS · ARTICLE", metadata=f"Story {index + 1:02d}",
                      pages=(f"Story {index + 1:02d}. This synthetic article was opened by a simulator tap through the Mac panel controller.",))


def exact_reader_text(feature):
    """Synthetic source with visible landmarks and loss-sensitive whitespace."""
    return (
        f"  {feature.upper()} BEGIN. 漢字 😀 e\u0301\r\n\r\n"
        + "".join(
            f"Segment {index:02d}: ASCII  spaces\t漢字かな 😀🙂 e\u0301 👩\u200d💻 "
            + "keep every word. " * 3 + ("\r\n" if index % 2 else "\r")
            for index in range(1, 9)
        )
        + "UNBROKEN_" + "abc漢😀" * 28 + "\n"
        + "Last wide row: 漢字かな 😀🙂 e\u0301 👩\u200d💻\r\n"
        + f"FINAL {feature.upper()} MARKER.  "
    )


class ExactReaderMessages(helpers.FakeMessages):
    def __init__(self, body):
        super().__init__([("Synthetic reader", "Open the complete synthetic message", time.time())])
        self.body = body

    def thread_message_page(self, thread, *, limit=8, before_row_id=None):
        del thread, limit
        # A real older-page affordance produces the formerly truncated
        # conversation footer before the reader opens. No send is possible.
        return helpers.MessageHistoryPage(entries=(helpers.MessageEntry(
            self.body if before_row_id is None else "Earlier synthetic message.",
            time.time(), False, row_id=2 if before_row_id is None else 1, unread=True,
        ),), has_older=before_row_id is None)


class ExactReaderNews(FixtureNews):
    def __init__(self, body):
        super().__init__()
        self.body = body

    async def open(self, index):
        self.opened.append(index)
        # Production pagination and Reader own the source-to-wire split.
        return Reader(tab="news", title="NEWS · ARTICLE", metadata=f"Story {index + 1:02d}",
                      pages=paginate(self.body))


class ProbeComplete(Exception):
    """End the short fixture diagnostic while retaining normal cleanup."""


async def wait_for(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return True
        await asyncio.sleep(.05)
    return False


async def gesture(action):
    await asyncio.to_thread(http, "/api/input", {"action": action})
    await asyncio.sleep(.2)


async def ui_events():
    payload = json.loads(await asyncio.to_thread(http, "/api/console"))
    prefix = "[joat repair] state "
    return [dict(json.loads(entry["message"][len(prefix):]), console_id=entry["id"])
            for entry in payload.get("entries", [])
            if entry.get("message", "").startswith(prefix)]


async def ui_mark():
    events = await ui_events()
    return events[-1]["console_id"] if events else -1


async def wait_ui(after, predicate, *, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = await ui_events()
        for event in reversed(events):
            if event["console_id"] > after and predicate(event):
                return event
        await asyncio.sleep(.1)
    raise TimeoutError("plugin did not finish the expected page transition")


async def capture(name):
    # State transitions are synchronized by controller receipts before this
    # pixel check; a stable frame alone is never a successful journey.
    previous = None
    stable_since = time.monotonic()
    deadline = stable_since + 5
    while time.monotonic() < deadline:
        raw = await asyncio.to_thread(http, "/api/screenshot/glasses")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != previous:
            previous, stable_since = digest, time.monotonic()
        if time.monotonic() - stable_since > .5:
            path = OUT / f"{name}.rgba.png"
            path.write_bytes(raw)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vf", "alphaextract", str(OUT / f"{name}.png")], check=True, timeout=3)
            return {"name": name, "sha256": digest, "file": f"{name}.png"}
        await asyncio.sleep(.15)
    raise TimeoutError(f"unstable pixels: {name}")


async def exact_wire_events():
    payload = json.loads(await asyncio.to_thread(http, "/api/console"))
    prefix = "[joat repair] exact-wire "
    return [dict(json.loads(entry["message"][len(prefix):]), console_id=entry["id"])
            for entry in payload.get("entries", [])
            if entry.get("message", "").startswith(prefix)]


def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rows_sha256(rows):
    return text_sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))


def reader_counter(page, *, strict=True):
    items = page["surface"].get("items", [])
    metadata = items[0] if items else ""
    pattern = r"^(\d+)/(\d+) · " if strict else r"(?<!\d)(\d+)/(\d+)(?!\d)"
    match = re.search(pattern, metadata)
    return [int(match[1]), int(match[2])] if match else None


async def run_exact_readers(router, panels, gateway, inputs, result, sources):
    """Real input and production pages; receipts do not certify glyph pixels."""
    result["exact_reader_gestures"] = []
    metrics = result["metrics"]["exact_readers"] = {}

    async def step(feature, action, predicate):
        mark = await ui_mark()
        start = len(inputs)
        receipt = {"feature": feature, "action": action, "after_console_id": mark, "completed": False}
        result["exact_reader_gestures"].append(receipt)
        started = time.monotonic()
        try:
            response = await asyncio.to_thread(http, "/api/input", {"action": action})
            receipt["simulator_response_sha256"] = hashlib.sha256(response).hexdigest()
            try:
                receipt["simulator_response"] = json.loads(response)
            except (ValueError, UnicodeError):
                receipt["simulator_response_bytes"] = len(response)
            await asyncio.sleep(.2)
            page = await wait_ui(mark, predicate)
            receipt.update(completed=True, ui_console_id=page["console_id"], title=page.get("title"))
            result["ui_receipts"].append(page)
            return page
        finally:
            receipt["mac_inputs"] = inputs[start:]
            receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)

    def is_page(title):
        return lambda event: event.get("title") == title and event["open"] and event.get("result") is not False

    async def select_second(feature, title):
        return await step(feature, "down", lambda event: is_page(title)(event)
                          and event["selection"] == 1
                          and event["method"] in ("hud.setSelection", "handleList"))

    for feature, title, parent in (
        ("message", "MESSAGES · MESSAGE", "MESSAGES · CONVERSATION"),
        ("news", "NEWS · ARTICLE", "NEWS"),
        ("note", "QUICKNOTES · DETAIL", "QUICKNOTES"),
    ):
        if args.exact_feature and feature not in args.exact_feature:
            continue
        source = sources[feature]
        entry = metrics[feature] = {
            "source_sha256": text_sha256(source), "source_characters": len(source),
            "pages": [], "accepted": False,
        }
        try:
            mark = await ui_mark()
            if feature == "news":
                await panels.publish("news", activate=True)
                opened = await wait_ui(mark, lambda event: event["method"] == "openPanelTab" and is_page("NEWS")(event))
                picker = "NEWS"
            else:
                picker = "MESSAGES" if feature == "message" else "QUICKNOTES"
                await router.open("Messages" if feature == "message" else "QuickNotes",
                                  request_id="voice-exact-reader-" + feature)
                opened = await wait_ui(mark, is_page(picker))
            result["ui_receipts"].append(opened)
            await select_second(feature, picker)
            if feature == "message":
                conversation = await step(feature, "click", is_page(parent))
                hint = conversation["surface"].get("options", {}).get("hint", "")
                entry["conversation_footer"] = hint
                entry["conversation_footer_complete"] = hint == "down older · tap read · say reply · left x2: back"
                result["frames"].append(await capture("exact-message-conversation"))
                if not args.message_first_tap and conversation["selection"] != 1:
                    await select_second(feature, parent)
            page = await step(feature, "click", is_page(title))
            for ordinal in range(24):
                items = page["surface"].get("items", [])
                counter = reader_counter(page)
                # Old metadata may put the counter last. Follow its gestures
                # for comparison, but never call that the new exact contract.
                navigation_counter = counter or reader_counter(page, strict=False)
                wires = [wire for wire in await exact_wire_events()
                         if wire["console_id"] < page["console_id"] and wire.get("title") == title
                         and wire.get("items", [None])[0] == (items[0] if items else None)]
                wire = wires[-1] if wires else {}
                rows, received_rows = items[1:], wire.get("items", [])[1:]
                footer = page["surface"].get("options", {}).get("hint", "")
                expected = []
                if navigation_counter and navigation_counter[0] > 1:
                    expected.append("up previous")
                if navigation_counter and navigation_counter[0] < navigation_counter[1]:
                    expected.append("down more")
                if feature == "message":
                    expected.append("say reply")
                expected.append("left x2: back")
                entry["pages"].append({
                    "counter": counter, "metadata": items[0] if items else None,
                    "ui_console_id": page["console_id"], "wire_console_id": wire.get("console_id"),
                    "surface_rows": rows, "received_rows": received_rows,
                    "surface_rows_sha256": rows_sha256(rows), "received_rows_sha256": rows_sha256(received_rows),
                    "footer": footer, "wire_footer": wire.get("hint"),
                    "footer_complete": footer == wire.get("hint") == " · ".join(expected),
                    "mac_wire_matched": any(frame.get("title") == title and frame.get("items") == wire.get("items")
                                            and frame.get("hint") == wire.get("hint") for frame in gateway.frames),
                })
                if ordinal == 0:
                    result["frames"].append(await capture(f"exact-{feature}-first"))
                if "down more" not in footer:
                    break
                if ordinal == 23:
                    entry["page_limit_reached"] = True
                    break
                old_items = items
                page = await step(feature, "down", lambda event: is_page(title)(event)
                                  and event["surface"].get("items") != old_items)
            result["frames"].append(await capture(f"exact-{feature}-last"))
            returned = await step(feature, "double_click", is_page(parent))
            result["frames"].append(await capture(f"exact-{feature}-back"))
            pages = entry["pages"]
            received = "".join(row for record in pages for row in record["received_rows"])
            surfaced = "".join(row for record in pages for row in record["surface_rows"])
            total = len(pages)
            forward_steps = [record for record in result["exact_reader_gestures"]
                             if record["feature"] == feature and record.get("title") == title and record["action"] == "down"]
            entry.update({
                "received_sha256": text_sha256(received), "surface_sha256": text_sha256(surfaced),
                "received_characters": len(received), "exact_wire_text_reached": received == source == surfaced,
                "page_counters_honest": [record["counter"] for record in pages] == [[index, total] for index in range(1, total + 1)],
                "final_marker_reached": f"FINAL {feature.upper()} MARKER." in "".join(pages[-1]["surface_rows"]),
                "footer_complete": all(record["footer_complete"] for record in pages),
                "received_matches_mac": all(record["mac_wire_matched"] for record in pages),
                "forward_gestures_matched": len(forward_steps) == total - 1 and all(
                    len([event for event in record["mac_inputs"] if event["container"] == "cappage"]) == 1
                    for record in forward_steps),
                "back_highlight_matches_target": returned["selection"] == 1 and returned["surface"].get("selectedIndex") == 1,
            })
            entry["accepted"] = all(entry[key] for key in (
                "exact_wire_text_reached", "page_counters_honest", "final_marker_reached", "footer_complete",
                "received_matches_mac", "forward_gestures_matched", "back_highlight_matches_target",
            )) and entry.get("conversation_footer_complete", True)
        except Exception as error:
            # Retain every attempted family, including meaningful old-source
            # failures; one failed reader must not erase the other evidence.
            entry["error"] = f"{type(error).__name__}: {error}"


async def main():
    gateway = ObservedGateway()
    connected = asyncio.Event()
    gateway.on_hello(lambda _hello: connected.set())
    capture_control = helpers.FakeVoiceCapture(state="recording", title="Synthetic saved note", active=args.journey not in ("note-reader", "exact-readers"))
    durable = DurableStore(TEMP / "notes.sqlite3")
    notes = NoteStore(durable) if args.journey in ("note-reader", "exact-readers") else None
    exact_sources = {feature: exact_reader_text(feature) for feature in ("message", "news", "note")}
    exact_note = "  Full QuickNote begins here.\r\n\r\n" + "Keep  both spaces and punctuation. " * 18 + "\n" + "unbroken" * 22 + "\nFINAL NOTE MARKER.  "
    if args.journey == "exact-readers":
        exact_note = exact_sources["note"]
    if notes is not None:
        for ordinal, text in enumerate((exact_note, "First row, different note.")):
            notes.create(note_id=f"synthetic-{ordinal}", idempotency_key=f"synthetic-{ordinal}",
                text=text, source=NoteSource(kind="synthetic"), now_us=100 + ordinal)
    replies = helpers.FakeReplies()
    news = ExactReaderNews(exact_sources["news"]) if args.journey == "exact-readers" else FixtureNews()
    messages = ExactReaderMessages(exact_sources["message"]) if args.journey == "exact-readers" else helpers.PagedHistoryMessages()
    panels = PanelPublisher(gateway, {"news": news})
    router = CapabilityRouter(hud=HudWriter(gateway), transport=gateway,
        status=StatusPublisher(TEMP / "status.json"), messages=messages,
        cards=CardLog(), replies=replies, voice_capture=capture_control, panels=panels, notes=notes)
    inputs = []

    def receive(event):
        inputs.append({"container": event.container_name, "name": event.selected_name,
                       "index": event.selected_index, "type": event.event_type,
                       "request_id": event.request_id})
        if args.journey == "exact-readers":
            inputs[-1]["trace_id"] = event.trace_id
        router.on_input(event)

    gateway.on_input(receive)
    root = _prepare_simulator_plugin_root(REPO, TEMP, "ws://127.0.0.1:18896/glasses")
    if args.baseline:
        for name in ("CapabilityMenu.ts", "HudPresenter.ts", "HudCanvasRenderer.ts"):
            (root / "src" / name).write_bytes(subprocess.check_output(
                ["git", "show", f"8386bf5f4a69239c7f7ee84a709cf47701ea242c:apps/hub-plugin/src/{name}"], cwd=REPO))
    # Retain source identity before disposable fixture instrumentation. A
    # capture can then be traced to working bytes rather than a Git label.
    copied_sources = {}
    for folder, prefix in ((root / "src", "apps/hub-plugin/src"),
                           (mac_root / "joat", "apps/mac/joat")):
        for copied in sorted(folder.rglob("*")):
            if copied.is_file() and copied.suffix in (".ts", ".py"):
                key = prefix + "/" + copied.relative_to(folder).as_posix()
                copied_sources[key] = hashlib.sha256(copied.read_bytes()).hexdigest()
    main_file = root / "src/main.ts"
    source = main_file.read_text()
    source = source.replace("import { AudioInputSource }", "import { AudioInputSource, OsEventTypeList, EventSourceType }")
    anchor = "  await client.init();"
    assert source.count(anchor) == 1
    source = source.replace(anchor, anchor + """
  const auditBridge = client.require();
  auditBridge.audioControl = async () => false;
  const auditSubscribe = auditBridge.onEvenHubEvent.bind(auditBridge);
  auditBridge.onEvenHubEvent = (callback) => auditSubscribe((event) => {
    if (event.sysEvent?.eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
      console.info('[joat repair] back-source ' + JSON.stringify({
        original: event.sysEvent.eventSource, fixture: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
      }));
      callback({...event, sysEvent: {...event.sysEvent, eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L}});
    } else callback(event);
  });
  const auditRebuild = auditBridge.rebuildPageContainer.bind(auditBridge);
  auditBridge.rebuildPageContainer = async (...args) => {
    const result = await auditRebuild(...args);
    console.info('[joat repair] rebuild ' + JSON.stringify({nativeList: !!args[0].listObject?.length, result}));
    return result;
  };
""")
    if args.capture_render_input:
        source = source.replace(anchor, anchor + """
  // Read-only synthetic-fixture observation of the actual renderer input.
  // Return the original bytes; do not alter the canvas or conversion.
  const auditTextCalls = new WeakMap<CanvasRenderingContext2D, object[]>();
  const originalFillText = CanvasRenderingContext2D.prototype.fillText;
  CanvasRenderingContext2D.prototype.fillText = function(...args: Parameters<typeof originalFillText>) {
    if (this.canvas.width === 576 && this.canvas.height === 288) {
      const calls = auditTextCalls.get(this) ?? [];
      calls.push({text: args[0], x: args[1], y: args[2], font: this.font,
        align: this.textAlign, width: this.measureText(args[0]).width, color: this.fillStyle});
      auditTextCalls.set(this, calls);
    }
    return originalFillText.apply(this, args);
  };
  const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function(...args: Parameters<typeof originalGetImageData>) {
    const pixels = originalGetImageData.apply(this, args);
    if (args[0] === 0 && args[1] === 0 && args[2] === 576 && args[3] === 288
        && this.canvas.width === 576 && this.canvas.height === 288) {
      console.info('[joat render input] ' + this.canvas.toDataURL('image/png'));
      console.info('[joat typography] ' + JSON.stringify(auditTextCalls.get(this) ?? []));
    }
    return pixels;
  };
""")
    menu_anchor = "  const menu = new CapabilityMenu(hud, settings, link);"
    assert source.count(menu_anchor) == 1
    source = source.replace(menu_anchor, menu_anchor + """
  const auditOnMessage = link.onMessage.bind(link);
  link.onMessage = (handler) => auditOnMessage(async (message, context) => {
    console.info('[joat repair] downlink-start ' + JSON.stringify({kind: message.kind}));
    try { await handler(message, context); }
    finally { console.info('[joat repair] downlink-end ' + JSON.stringify({kind: message.kind})); }
  });
  const auditConnect = link.connect.bind(link);
  link.connect = () => {
    auditConnect();
    (link as any).socket?.addEventListener('message', (event: MessageEvent) => {
      console.info('[joat repair] wire ' + JSON.stringify({kind: JSON.parse(String(event.data)).kind}));
    });
  };
  // Read-only observers in the disposable fixture. Original methods still
  // own all state transitions and receive the same arguments/results.
  for (const method of ['presentList', 'presentLifecycle', 'openPanelTab', 'toggle']) {
    const original = (menu as any)[method].bind(menu);
    (menu as any)[method] = async (...args: any[]) => {
      const result = await original(...args);
      console.info('[joat repair] state ' + JSON.stringify({
        method, result, open: menu.isOpen(), title: (menu as any).dataSurface?.title,
        family: (menu as any).pendingTitle,
        view: (menu as any).view, selection: (menu as any).selectedIndex,
        surface: hud.exportBackgroundState().surface,
      }));
      return result;
    };
  }
""")
    if args.journey == "exact-readers":
        wire_anchor = "    console.info('[joat repair] downlink-start ' + JSON.stringify({kind: message.kind}));"
        assert source.count(wire_anchor) == 1
        source = source.replace(wire_anchor, wire_anchor + """
    if (message.kind === 'hud.list') {
      console.info('[joat repair] exact-wire ' + JSON.stringify(message));
    }
""")
        # Only observe the original selection method after its paint settles.
        # No renderer, model, page data or event result is substituted.
        source = source.replace(menu_anchor, menu_anchor + """
  const exactSelectionState = (method: string, result: unknown) => {
    console.info('[joat repair] state ' + JSON.stringify({
      method, result, open: menu.isOpen(), title: (menu as any).dataSurface?.title,
      family: (menu as any).pendingTitle, view: (menu as any).view,
      selection: (menu as any).selectedIndex, surface: hud.exportBackgroundState().surface,
    }));
  };
  const exactSetSelection = (hud as any).setSelection.bind(hud);
  (hud as any).setSelection = async (...args: any[]) => {
    const result = await exactSetSelection(...args);
    exactSelectionState('hud.setSelection', result);
    return result;
  };
  // Historical native lists report cursor movement as a ListEvent. Preserve
  // this synchronous boolean contract when collecting the old-source run.
  const exactHandleList = menu.handleList.bind(menu);
  menu.handleList = (event) => {
    const result = exactHandleList(event);
    exactSelectionState('handleList', result);
    return result;
  };
""")
    if args.raw_gray8_probe:
        encoder_anchor = "        encodeGray8TilePng,"
        assert source.count(encoder_anchor) == 1
        source = source.replace(encoder_anchor, "        async (tile) => tile.pixels,")
    if args.packed_gray4_probe:
        assert not args.raw_gray8_probe
        encoder_anchor = "        encodeGray8TilePng,"
        assert source.count(encoder_anchor) == 1
        source = source.replace(encoder_anchor, """
        async (tile) => {
          const packed = new Uint8Array(tile.pixels.length / 2);
          for (let i = 0; i < tile.pixels.length; i += 2) {
            packed[i / 2] = (Math.round(tile.pixels[i] / 17) << 4)
              | Math.round(tile.pixels[i + 1] / 17);
          }
          return packed;
        },""")
    main_file.write_text(source)
    if args.luma_probe:
        renderer_file = root / "src/HudCanvasRenderer.ts"
        renderer_source = renderer_file.read_text()
        gray_anchor = "Math.max(rgba[source] ?? 0, rgba[source + 1] ?? 0, rgba[source + 2] ?? 0)"
        assert renderer_source.count(gray_anchor) == 1
        renderer_file.write_text(renderer_source.replace(gray_anchor,
            "Math.round(0.2126 * (rgba[source] ?? 0) + 0.7152 * (rgba[source + 1] ?? 0) + 0.0722 * (rgba[source + 2] ?? 0))"))
    processes = []
    result = {"baseline": args.baseline, "journey": args.journey, "scope": "synthetic capture/messages/news; production Mac+Hub+gateway; real simulator taps/swipes; fixture-normalized left Back source; injected feature entry; no microphone, note persistence or real sends", "frames": [], "metrics": {}, "ui_receipts": [], "baseline_ref": "8386bf5f4a69239c7f7ee84a709cf47701ea242c" if args.baseline else None}
    result["copied_source_sha256_before_fixture_instrumentation"] = copied_sources
    result["diagnostic_wire_format_override"] = "raw-gray8" if args.raw_gray8_probe else None
    if args.packed_gray4_probe:
        result["diagnostic_wire_format_override"] = "packed-gray4-high-nibble-first"
    result["capture_render_input"] = args.capture_render_input
    result["diagnostic_grayscale_override"] = "weighted-srgb" if args.luma_probe else None
    if args.journey == "exact-readers":
        result["acceptance_runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        result["scope"] = (
            "synthetic ASCII/CJK/emoji/combining/tabs/CRLF/CR/LF through production Mac Message/News/Note "
            "readers and production Hub; real simulator gestures; fixture-normalized left Back; "
            "injected feature entry; temporary NoteStore; no live microphone, messages or sends; "
            "wire/surface acceptance is separate from visual inspection and physical-G2 proof"
        )
    result["simulator_binary_sha256"] = hashlib.sha256(SIM.read_bytes()).hexdigest()
    result["simulator_entry_url"] = "http://127.0.0.1:5395/"
    result["git_head_at_capture"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    await gateway.start()
    try:
        with (OUT / "vite.log").open("w") as vite_log, (OUT / "native.log").open("w") as sim_log:
            vite = subprocess.Popen([str(REPO / "apps/hub-plugin/node_modules/.bin/vite"), "--host", "127.0.0.1", "--port", "5395", "--strictPort"], cwd=root, env={**os.environ, "VITE_MAC_WS_URL": "ws://127.0.0.1:18896/glasses"}, stdout=vite_log, stderr=subprocess.STDOUT)
            processes.append(vite)
            await asyncio.sleep(.7)
            sim = subprocess.Popen([str(SIM), "http://127.0.0.1:5395/", "--automation-port", "9795", "--no-glow"], env={**os.environ, "RUST_LOG": "debug"}, stdout=sim_log, stderr=subprocess.STDOUT)
            processes.append(sim)
            await asyncio.wait_for(connected.wait(), timeout=10 if args.probe_switch else 20)
            await asyncio.sleep(3)

            if args.manual_review:
                result["manual_review"] = True
                result["visual_acceptance"] = "pending direct inspection"
                (OUT / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
                print("Manual simulator ready. Commands: News, Messages, QuickNotes, capture LABEL, quit", flush=True)
                while True:
                    try:
                        command = (await asyncio.to_thread(input, "review> ")).strip()
                    except EOFError:
                        break
                    if command == "quit":
                        break
                    if command in ("News", "Messages", "QuickNotes"):
                        await router.open(command, request_id="voice-manual-" + command.lower())
                    elif re.fullmatch(r"capture [a-z0-9-]+", command):
                        result["frames"].append(await capture(command.split()[1]))
                        print("Captured current simulator framebuffer.", flush=True)
                    else:
                        print("Use News, Messages, QuickNotes, capture LABEL, or quit.", flush=True)
                raise ProbeComplete()

            if args.probe_switch:
                for feature, title in (("QuickNotes", "QUICKNOTES · RECORDING"), ("Messages", "MESSAGES")):
                    mark = await ui_mark()
                    await router.open(feature, request_id="voice-probe-" + feature.lower())
                    result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == title and e["open"], timeout=5))
                    result["frames"].append(await capture("probe-" + feature.lower()))
                    if args.probe_save and feature == "QuickNotes":
                        mark = await ui_mark()
                        await gesture("click")
                        result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "QUICKNOTES · SETUP" and e["open"], timeout=5))
                        result["frames"].append(await capture("probe-saved"))
                raise ProbeComplete()

            if args.journey == "exact-readers":
                await run_exact_readers(router, panels, gateway, inputs, result, exact_sources)

            if args.journey == "note-reader":
                mark = await ui_mark()
                await router.open("QuickNotes", request_id="voice-repair-reader")
                result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "QUICKNOTES" and e["open"]))
                result["frames"].append(await capture("reader-picker"))
                mark = await ui_mark()
                for action in ("down", "click"):
                    await gesture(action)
                page = await wait_ui(mark, lambda e: e.get("title") == "QUICKNOTES · DETAIL" and e["open"])
                bodies = []
                displayed_pages = []
                displayed_total = None
                consistent_total = True
                for ordinal in range(20):
                    result["ui_receipts"].append(page)
                    result["frames"].append(await capture(f"reader-page-{ordinal + 1}"))
                    items = page["surface"].get("items", [])
                    counter = re.match(r"^(\d+)/(\d+) · ", items[0] if items else "")
                    displayed_pages.append(int(counter[1]) if counter else None)
                    if counter is None:
                        consistent_total = False
                    elif displayed_total is None:
                        displayed_total = int(counter[2])
                    else:
                        consistent_total = consistent_total and displayed_total == int(counter[2])
                    bodies.append("".join(items[1:] if counter else items))
                    if counter is None or counter[1] == counter[2]:
                        break
                    mark = await ui_mark()
                    await gesture("down")
                    page = await wait_ui(mark, lambda e: e.get("title") == "QUICKNOTES · DETAIL" and e["open"])
                mark = await ui_mark()
                await gesture("double_click")
                returned = await wait_ui(mark, lambda e: e.get("title") == "QUICKNOTES" and e["open"])
                result["ui_receipts"].append(returned)
                result["frames"].append(await capture("reader-back"))
                reconstructed = "".join(bodies)
                result["metrics"]["Q2"] = {
                    "source_characters": len(exact_note), "received_characters": len(reconstructed),
                    "pages": len(bodies), "exact_wire_text_reached": reconstructed == exact_note,
                    "displayed_pages": displayed_pages, "displayed_total": displayed_total,
                    "page_counters_honest": consistent_total
                        and displayed_total == len(displayed_pages)
                        and displayed_pages == list(range(1, len(displayed_pages) + 1)),
                    "back_highlight_matches_target": returned["selection"] == 1 and returned["surface"].get("selectedIndex") == 1,
                    "source_sha256": hashlib.sha256(exact_note.encode()).hexdigest(),
                    "received_sha256": hashlib.sha256(reconstructed.encode()).hexdigest(),
                }

            if args.journey in ("all", "quicknotes"):
                mark = await ui_mark()
                await router.open("QuickNotes", request_id="voice-repair-notes")
                result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "QUICKNOTES · RECORDING" and e["open"]))
                result["frames"].append(await capture("q1-recording"))
                mark = await ui_mark()
                for action in ("down", "down", "click"):
                    await gesture(action)
                assert await wait_for(lambda: capture_control.discard_pending), "discard request did not reach Mac"
                result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "QUICKNOTES · DISCARD?" and e["open"]))
                await asyncio.sleep(6.3)
                result["frames"].append(await capture("q1-discard"))
                mark = await ui_mark()
                await gesture("double_click")
                result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "QUICKNOTES · RECORDING" and e["open"]))
                await asyncio.sleep(6.3)
                result["frames"].append(await capture("q1-after-back"))
                mark = await ui_mark()
                for action in ("up", "up", "click"):
                    await gesture(action)
                assert await wait_for(lambda: not capture_control.active), "final note action did not reach Mac"
                result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "QUICKNOTES · SETUP" and e["open"]))
                await asyncio.sleep(6.3)
                result["frames"].append(await capture("q1-result"))
                result["metrics"]["Q1"] = {"actions": capture_control.calls, "save_dispatched_without_confirm_discard": capture_control.calls == ["discard", "keep", "stop"], "persistence_verified": False}

            if args.journey in ("all", "messages"):
                mark = await ui_mark()
                await router.open("Messages", request_id="voice-repair-messages")
                result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "MESSAGES" and e["open"]))
                result["frames"].append(await capture("m3-picker-first"))
                mark = await ui_mark()
                for action in ("down", "click"):
                    await gesture(action)
                assert await wait_for(lambda: router._message_history is not None), "conversation did not open"
                result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "MESSAGES · CONVERSATION" and e["open"]))
                result["frames"].append(await capture("m2-latest"))
                mark = await ui_mark()
                for _ in range(8):
                    await gesture("down")
                older = router._message_history is not None and router._message_history.index == 1
                if older:
                    result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "MESSAGES · CONVERSATION" and e["surface"].get("items", [""])[0].startswith("Older")))
                result["frames"].append(await capture("m2-after-older"))
                mark = await ui_mark()
                for _ in range(8):
                    await gesture("up")
                newer = router._message_history is not None and router._message_history.index == 0
                if older and newer:
                    result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "MESSAGES · CONVERSATION" and e["surface"].get("items", [""])[0].startswith("Latest")))
                result["frames"].append(await capture("m2-after-newer"))
                result["metrics"]["M2"] = {"older_reached": older, "newer_returned": older and newer,
                    "page_requests": sum(e["container"] == "cappage" for e in inputs)}
                mark = await ui_mark()
                await gesture("double_click")
                picker_return = await wait_ui(mark, lambda e: e.get("title") == "MESSAGES" and e["open"])
                result["ui_receipts"].append(picker_return)
                result["frames"].append(await capture("m3-picker-return"))
                selected_before = len(replies.selected)
                mark = await ui_mark()
                await gesture("click")
                if await wait_for(lambda: len(replies.selected) > selected_before, timeout=1.5):
                    result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "MESSAGES · CONVERSATION" and e["open"]))
                result["frames"].append(await capture("m3-second-selection"))
                result["metrics"]["M3"] = {"additional_selections": len(replies.selected)-selected_before,
                    "history_reopened": router._message_history is not None,
                    "highlight_matches_target": picker_return["surface"].get("selectedIndex") == picker_return["selection"]}

            if args.journey in ("all", "news"):
                mark = await ui_mark()
                await panels.publish("news", activate=True)
                news_open = await wait_ui(mark, lambda e: e["method"] == "openPanelTab")
                result["ui_receipts"].append(news_open)
                result["frames"].append(await capture("n3-list"))
                mark = await ui_mark()
                for action in ("down", "click"):
                    await gesture(action)
                article = None
                if await wait_for(lambda: bool(news.opened), timeout=1.5):
                    article = await wait_ui(mark, lambda e: e.get("title") == "NEWS · ARTICLE" and e["open"])
                    result["ui_receipts"].append(article)
                result["frames"].append(await capture("n3-article"))
                mark = await ui_mark()
                await gesture("double_click")
                returned = await wait_ui(mark, lambda e: e["method"] == "toggle")
                result["ui_receipts"].append(returned)
                result["frames"].append(await capture("n3-back"))
                if article:
                    mark = await ui_mark()
                    await gesture("click")
                    assert await wait_for(lambda: len(news.opened) == 2), "News did not remain selectable after Back"
                    result["ui_receipts"].append(await wait_ui(mark, lambda e: e.get("title") == "NEWS · ARTICLE" and e["open"]))
                result["metrics"]["N3"] = {"opened_indices": news.opened,
                    "selected_article_opened": article is not None and news.opened == [1, 1],
                    "back_restored_list": returned.get("title") == "NEWS" and returned["open"]}
    except ProbeComplete:
        result["probe_passed"] = True
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        try:
            (OUT / "console.json").write_bytes(await asyncio.to_thread(http, "/api/console"))
        except Exception:
            pass
        result["inputs"] = inputs
        result["mac_frames"] = gateway.frames
        (OUT / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
        for process in reversed(processes):
            stop(process)
        await gateway.stop()
        durable.close()
        scratch.cleanup()
    print(json.dumps({k: v for k, v in result.items() if k not in ("inputs", "mac_frames")}, indent=2))
    if "error" in result:
        raise SystemExit(1)
    if args.probe_switch or args.manual_review:
        return
    checks = []
    metrics = result["metrics"]
    if "Q1" in metrics:
        checks.append(metrics["Q1"]["save_dispatched_without_confirm_discard"])
    if "M2" in metrics:
        checks.extend((metrics["M2"]["older_reached"], metrics["M2"]["newer_returned"],
            metrics["M3"]["history_reopened"], metrics["M3"]["additional_selections"] == 1,
            metrics["M3"]["highlight_matches_target"]))
    if "N3" in metrics:
        checks.extend((metrics["N3"]["selected_article_opened"], metrics["N3"]["back_restored_list"]))
    if "Q2" in metrics:
        checks.extend((metrics["Q2"]["exact_wire_text_reached"], metrics["Q2"]["page_counters_honest"],
            metrics["Q2"]["back_highlight_matches_target"]))
    if "exact_readers" in metrics:
        checks.extend(metrics["exact_readers"].get(feature, {}).get("accepted", False)
                      for feature in (args.exact_feature or ("message", "news", "note")))
    if not args.baseline and (not checks or not all(checks)):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
