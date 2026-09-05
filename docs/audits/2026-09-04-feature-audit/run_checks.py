"""Bounded audit only: isolated fixtures, current source, no external sends."""
from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "evidence"
PYTHON = REPO / ".venv/bin/python"
# The npm shim uses execFileSync. Terminating that shim can orphan the GUI
# child and reuse its old automation server. Own the installed native process.
SIM = Path.home() / ".local/lib/node_modules/@evenrealities/evenhub-simulator/node_modules/@evenrealities/sim-darwin-arm64/bin/evenhub-simulator"
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "apps/mac"))


def emit(value):
    print(json.dumps(value), flush=True)


def http(url, body=None):
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.read()


def ready(url, process, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("audit child exited before readiness")
        try:
            http(url)
            return
        except Exception:
            time.sleep(.15)
    raise TimeoutError("audit child readiness")


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def capture(base, destination):
    # Read pixels after startup and require stability, independently of logging.
    from simulator_navigation_visual_acceptance import png_rgba_alpha
    previous = None
    since = time.monotonic()
    deadline = since + 18
    while time.monotonic() < deadline:
        raw = http(base + "/api/screenshot/glasses")
        width, height, alpha = png_rgba_alpha(raw)
        digest = hashlib.sha256(raw).hexdigest()
        now = time.monotonic()
        if digest != previous:
            previous, since = digest, now
        if sum(x > 0 for x in alpha) > 120 and now - since > .8:
            destination.write_bytes(raw)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(destination),
                            "-vf", "alphaextract", str(destination.with_suffix("").with_suffix(".png"))], check=True)
            return {"sha256": digest, "width": width, "height": height,
                    "lit_pixels": sum(x > 0 for x in alpha), "file": str(destination.relative_to(OUT))}
        time.sleep(.2)
    raise TimeoutError("framebuffer never became nonempty and stable")


def command(name, argv, cwd, timeout=180):
    path = OUT / (name + ".log")
    started = time.monotonic()
    with path.open("w") as log:
        try:
            completed = subprocess.run(argv, cwd=cwd, env={**os.environ, "PYTHONPATH": str(REPO / "apps/mac")},
                                       stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            code = completed.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    result = {"name": name, "exit_code": code, "seconds": round(time.monotonic()-started, 2), "log": path.name}
    emit(result)
    return result


def wal_probe():
    from joat.channels.imessage import IMessageStore
    with tempfile.TemporaryDirectory(prefix="joat-audit-wal-") as scratch:
        path = Path(scratch) / "chat.db"
        writer = sqlite3.connect(path)
        writer.execute("create table message(text text)")
        writer.commit()
        writer.execute("pragma journal_mode=WAL")
        writer.execute("pragma wal_autocheckpoint=0")
        writer.execute("insert into message values ('synthetic audit row')")
        writer.commit()
        with IMessageStore(path)._connect() as reader:
            actual = reader.execute("select count(*) from message").fetchone()[0]
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as reader:
            baseline = reader.execute("select count(*) from message").fetchone()[0]
        result = {"scope": "synthetic live WAL only", "production_read_rows": actual,
                  "ordinary_readonly_rows": baseline, "writer_rows": 1}
        writer.close()
    return result


def visual_gallery():
    states = ["overview", "messages", "messages-conversation", "message-reader", "messages-compose",
              "notifications", "notification-detail", "quicknotes", "quicknote-detail",
              "quicknote-recording", "quicknote-discard", "brainstorm-sessions", "brainstorm-ready",
              "brainstorm-live", "terminal-sessions", "agent-terminal-ready", "agent-terminal-blocked",
              "panel-news", "panel-news-expanded", "panel-article", "panel-pomodoro",
              "panel-pomodoro-expanded", "assistant-overlay", "nav-destination", "nav-guidance", "nav-error"]
    destination = OUT / "gallery"
    destination.mkdir(exist_ok=True)
    results = []
    from simulator_brainstorm_action_flow_acceptance import _prepare_simulator_plugin_root
    with tempfile.TemporaryDirectory(prefix="joat-audit-gallery-") as scratch:
        root = _prepare_simulator_plugin_root(REPO, Path(scratch), "ws://127.0.0.1:18894/glasses")
        with open(os.devnull, "wb") as sink:
            vite = subprocess.Popen([str(REPO / "apps/hub-plugin/node_modules/.bin/vite"), "--host", "127.0.0.1", "--port", "5294", "--strictPort"], cwd=root, stdout=sink, stderr=sink)
            try:
                ready("http://127.0.0.1:5294/", vite)
                for state in states:
                    with socket.socket() as probe:
                        if probe.connect_ex(("127.0.0.1", 9994)) == 0:
                            raise RuntimeError("refusing to reuse an existing simulator automation server")
                    sim = subprocess.Popen([str(SIM), f"http://127.0.0.1:5294/?joatVisual={state}", "--automation-port", "9994", "--no-glow"], stdout=sink, stderr=sink)
                    try:
                        ready("http://127.0.0.1:9994/api/ping", sim)
                        time.sleep(3)
                        record = {"state": state, "evidence": "official simulator fixture pixels only",
                                  **capture("http://127.0.0.1:9994", destination / f"{state}.rgba.png")}
                        console = json.loads(http("http://127.0.0.1:9994/api/console"))
                        record["error_count"] = sum(e.get("level") == "error" for e in console.get("entries", []))
                    except Exception as error:
                        record = {"state": state, "error": type(error).__name__ + ": " + str(error)}
                    finally:
                        stop(sim)
                    results.append(record)
                    emit(record)
            finally:
                stop(vite)
    return results


async def messages_flow(flow):
    from simulator_brainstorm_action_flow_acceptance import _prepare_simulator_plugin_root
    from simulator_messages_flow_acceptance import run
    destination = OUT / ("messages-" + flow)
    destination.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="joat-audit-messages-") as scratch:
        root = _prepare_simulator_plugin_root(REPO, Path(scratch), "ws://127.0.0.1:18894/glasses")
        env = {**os.environ, "VITE_MAC_WS_URL": "ws://127.0.0.1:18894/glasses"}
        processes = []
        with open(os.devnull, "wb") as sink:
            try:
                vite = subprocess.Popen([str(REPO / "apps/hub-plugin/node_modules/.bin/vite"), "--host", "127.0.0.1", "--port", "5294", "--strictPort"], cwd=root, env=env, stdout=sink, stderr=sink)
                processes.append(vite)
                await asyncio.to_thread(ready, "http://127.0.0.1:5294/", vite)
                task = asyncio.create_task(run(18894, flow, "accepted", automation_base="http://127.0.0.1:9994", evidence_dir=destination))
                await asyncio.sleep(.3)
                sim = subprocess.Popen([str(SIM), "http://127.0.0.1:5294/?joatTestOpenMenu=1", "--automation-port", "9994", "--no-glow"], stdout=sink, stderr=sink)
                processes.append(sim)
                try:
                    await asyncio.wait_for(task, 55)
                    result = {"flow": flow, "result": "passed", "scope": "isolated production routers; synthetic DB; direct command injection; recording sender"}
                except Exception as error:
                    result = {"flow": flow, "result": "incomplete", "error": type(error).__name__ + ": " + str(error)}
                    try:
                        result["last_frame"] = await asyncio.to_thread(capture, "http://127.0.0.1:9994", destination / "last-observed.rgba.png")
                        console = json.loads(http("http://127.0.0.1:9994/api/console"))
                        # This isolated fixture contains no wearer content or credentials.
                        (destination / "fixture-console.json").write_text(json.dumps(console, indent=2))
                    except Exception as capture_error:
                        result["capture_error"] = type(capture_error).__name__
            finally:
                for process in reversed(processes):
                    stop(process)
    emit(result)
    return result


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--visual-only", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out is not None:
        OUT = args.out.resolve()
    OUT.mkdir(exist_ok=True)
    result = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "source_sha256": {}}
    for base in (REPO / "apps/hub-plugin/src", REPO / "apps/mac/joat"):
        for path in sorted(base.rglob("*")):
            if path.suffix in (".ts", ".py"):
                result["source_sha256"][str(path.relative_to(REPO))] = hashlib.sha256(path.read_bytes()).hexdigest()
    result["checks"] = [] if args.visual_only else [command("plugin-typecheck", ["npm", "run", "typecheck"], REPO / "apps/hub-plugin"),
        command("plugin-tests", ["npm", "test"], REPO / "apps/hub-plugin"),
        command("python-focused", [str(PYTHON), "-m", "pytest", "-q", "tests/unit/test_capabilities.py",
            "tests/unit/test_message_reply.py", "tests/unit/test_imessage.py", "tests/unit/test_orchestrator.py",
            "tests/unit/test_agent_terminal.py", "tests/unit/test_pomodoro.py", "tests/unit/test_news.py",
            "tests/unit/test_cli_reply_engine.py", "tests/unit/test_voice_note_capture.py"], REPO)]
    result["synthetic_wal_probe"] = wal_probe()
    result["gallery"] = visual_gallery()
    result["messages_flows"] = [] if args.visual_only else [asyncio.run(messages_flow(flow)) for flow in ("reader", "history", "reply")]
    result["unique_gallery_frames"] = len({entry["sha256"] for entry in result["gallery"] if "sha256" in entry})
    result["gallery_requires_review"] = True
    result["limits"] = ["No real message sent", "No physical G2 acceptance", "No package built or uploaded",
                        "Gallery uses static fixtures; messages flows use synthetic data and direct command injection",
                        "Visual inspection required separately; test passing alone does not close reported gaps"]
    (OUT / "checks.json").write_text(json.dumps(result, indent=2) + "\n")
    emit({"complete": True, "receipt": str(OUT / "checks.json")})


if __name__ == "__main__":
    main()
