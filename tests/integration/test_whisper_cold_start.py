"""Controlled local subprocess + HTTP cold starts; no model, mic or G2.

JOAT_COLD_START_REAL_BUDGET=1 uses the production 30-second foreground gate.
The default runs the identical state transitions with a short test budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import textwrap
import time
from pathlib import Path

import pytest

from joat.audio import whisper_serve
from joat.audio.whisper_serve import DEFAULT_READY_TIMEOUT_S, WhisperServeEngine


STUB_CLI = """
import hashlib, json, os, signal, sys, time, wave
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

root = Path(__file__).parent
args = sys.argv[1:]
def arg(name):
    return args[args.index(name) + 1]
def record(**data):
    with (root / 'events.jsonl').open('a') as out:
        out.write(json.dumps(data) + '\\n')

if args[0] == 'transcribe':
    if (root / 'batch-ignore-term').exists():
        signal.signal(signal.SIGTERM, lambda *_: (root / 'batch-terminated').touch())
    with wave.open(arg('--audio-path'), 'rb') as wav:
        digest = hashlib.sha256(wav.readframes(wav.getnframes())).hexdigest()
    record(kind='batch', pid=os.getpid(), model=arg('--model'), directory=arg('--download-model-path'),
           audio_hash=digest)
    while (root / 'batch-wait').exists():
        time.sleep(0.01)
    print('batch result')
    sys.exit(0)

record(kind='spawn', pid=os.getpid(), model=arg('--model'),
       directory=arg('--download-model-path'))
failed = root / 'failed'
if (root / 'fail-first').exists() and not failed.exists():
    failed.touch()
    sys.exit(1)

if not (root / 'bind-early').exists():
    while not (root / 'ready').exists():
        time.sleep(0.01)

if (root / 'ignore-term').exists():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        ready = (root / 'ready').exists()
        record(kind='http', ready=ready, pid=os.getpid())
        payload = json.dumps({'text': 'resident result'} if ready else {'error': True}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *args):
        pass

HTTPServer((arg('--host'), int(arg('--port'))), Handler).serve_forever()
"""


def events(root: Path) -> list[dict[str, object]]:
    path = root / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def cold_engine(root: Path) -> WhisperServeEngine:
    script = root / "whisper-stub"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(STUB_CLI))
    script.chmod(0o700)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    budget = DEFAULT_READY_TIMEOUT_S if os.environ.get("JOAT_COLD_START_REAL_BUDGET") == "1" else 0.1
    return WhisperServeEngine(
        binary=str(script), model_dir=root / "models", port=port,
        ready_timeout_s=budget,
    )


@pytest.mark.parametrize("bind_early", [False, True])
async def test_cold_child_outlives_foreground_gate_and_replaces_temporary_batch(
    tmp_path: Path, bind_early: bool,
) -> None:
    if bind_early:
        (tmp_path / "bind-early").touch()
    engine = cold_engine(tmp_path)
    pcm = b"\x01\x02" * 8000
    started = time.monotonic()
    try:
        assert await engine.start()
        elapsed = time.monotonic() - started
        assert engine._ready_timeout_s <= elapsed < engine._ready_timeout_s + 2
        assert not engine._ready
        child = engine._proc
        assert child is not None and child.returncode is None
        await engine.feed(pcm)
        assert (await engine.flush()).text == "batch result"
        (tmp_path / "ready").touch()
        await asyncio.wait_for(asyncio.shield(engine._startup_task), 3)
        assert engine._proc is child  # kept the very same cold process
        await engine.feed(pcm)
        assert (await engine.flush()).text == "resident result"
        rows = events(tmp_path)
        assert len([row for row in rows if row["kind"] == "spawn"]) == 1
        batch = [row for row in rows if row["kind"] == "batch"]
        assert len(batch) == 1
        batch[0].pop("pid")
        assert batch == [{
            "kind": "batch", "model": "large-v3_turbo",
            "directory": str(tmp_path / "models"),
            "audio_hash": hashlib.sha256(pcm).hexdigest(),
        }]
    finally:
        child = engine._proc
        await engine.close()
        assert child is None or child.returncode is not None
        assert engine._proc is None


async def test_first_child_exits_then_recovers_without_restarting_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(whisper_serve, "START_RETRY_DELAY_S", 0.05)
    (tmp_path / "fail-first").touch()
    engine = cold_engine(tmp_path)
    engine._ready_timeout_s = 0.1
    try:
        assert await engine.start()
        (tmp_path / "ready").touch()
        await asyncio.wait_for(asyncio.shield(engine._startup_task), 4)
        await engine.feed(b"\x01\x02" * 8000)
        assert (await engine.flush()).text == "resident result"
        spawned = [row for row in events(tmp_path) if row["kind"] == "spawn"]
        assert len(spawned) == 2
        assert spawned[0]["pid"] != spawned[1]["pid"]
    finally:
        await engine.close()


@pytest.mark.parametrize("cancel_flush_during_cleanup", [False, True])
async def test_close_reaps_inflight_temporary_batch_and_resident_children(
    tmp_path: Path, cancel_flush_during_cleanup: bool,
) -> None:
    (tmp_path / "batch-wait").touch()
    if cancel_flush_during_cleanup:
        (tmp_path / "batch-ignore-term").touch()
    engine = cold_engine(tmp_path)
    engine._ready_timeout_s = 0.1
    flushing = None
    try:
        assert await engine.start()
        child = engine._proc
        await engine.feed(b"\x01\x02" * 8000)
        flushing = asyncio.create_task(engine.flush())
        async with asyncio.timeout(2):
            while not any(row["kind"] == "batch" for row in events(tmp_path)):
                await asyncio.sleep(0.01)
        batch = next(row for row in events(tmp_path) if row["kind"] == "batch")
        closing = asyncio.gather(engine.close(), engine.close())
        if cancel_flush_during_cleanup:
            async with asyncio.timeout(2):
                while not (tmp_path / "batch-terminated").exists():
                    await asyncio.sleep(0.01)
            flushing.cancel()
        await asyncio.wait_for(closing, 7)
        with pytest.raises(asyncio.CancelledError):
            await flushing
        assert child is not None and child.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(batch["pid"], 0)
        assert not engine._batch_tasks
    finally:
        await engine.close()
        if flushing is not None:
            await asyncio.gather(flushing, return_exceptions=True)


async def test_close_kills_and_reaps_child_that_ignores_termination(tmp_path: Path) -> None:
    (tmp_path / "ignore-term").touch()
    (tmp_path / "ready").touch()
    engine = cold_engine(tmp_path)
    engine._ready_timeout_s = 3
    try:
        assert await engine.start()
        assert engine._ready
        child = engine._proc
        await asyncio.wait_for(engine.close(), 7)
        assert child is not None and child.returncode == -9
        assert engine._proc is None
    finally:
        await engine.close()


async def test_shutdown_after_foreground_timeout_reaps_child_and_stops_retries(
    tmp_path: Path,
) -> None:
    engine = cold_engine(tmp_path)
    engine._ready_timeout_s = 0.1
    shutdown = asyncio.Event()
    try:
        assert await engine.start(shutdown=shutdown)
        child = engine._proc
        assert child is not None
        shutdown.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(engine._startup_task), 2)
        (tmp_path / "ready").touch()
        assert child.returncode is not None
        assert engine._proc is None
        assert not engine._ready
        assert not await engine.start()
        assert len([row for row in events(tmp_path) if row["kind"] == "spawn"]) == 1
    finally:
        await engine.close()
