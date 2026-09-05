"""Short, isolated simulator diagnostic; instruments only a temporary copy."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from run_checks import REPO, SIM, http, stop

sys.path.insert(0, str(REPO / "scripts"))
from simulator_brainstorm_action_flow_acceptance import _prepare_simulator_plugin_root

OUT = Path(__file__).resolve().parent / "evidence/news-transition"
OUT.mkdir(exist_ok=True)
started = time.monotonic()
processes = []
with tempfile.TemporaryDirectory(prefix="joat-audit-news-") as scratch:
    root = _prepare_simulator_plugin_root(REPO, Path(scratch), "ws://127.0.0.1:18895/glasses")
    main = root / "src/main.ts"
    source = main.read_text()
    anchor = "  await client.init();"
    assert source.count(anchor) == 1
    source = source.replace(anchor, anchor + """
  const auditBridge = client.require();
  const auditRebuild = auditBridge.rebuildPageContainer.bind(auditBridge);
  auditBridge.rebuildPageContainer = async (...args) => {
    const result = await auditRebuild(...args);
    console.info('[joat audit] rebuild ' + JSON.stringify({result, args}));
    return result;
  };
""")
    anchor = "    await renderVisualState(hud, menu, imu, VISUAL_STATE, navigation);"
    assert source.count(anchor) == 1
    source = source.replace(anchor, anchor + "\n    console.info('[joat audit] complete ' + JSON.stringify({open: menu.isOpen(), state: hud.exportBackgroundState()}));")
    main.write_text(source)
    with open(os.devnull, "wb") as sink:
        try:
            vite = subprocess.Popen([str(REPO / "apps/hub-plugin/node_modules/.bin/vite"), "--host", "127.0.0.1", "--port", "5394", "--strictPort"], cwd=root, stdout=sink, stderr=sink)
            processes.append(vite)
            deadline = time.monotonic() + 6
            while True:
                if vite.poll() is not None:
                    raise RuntimeError("private Vite exited")
                try:
                    http("http://127.0.0.1:5394/")
                    break
                except Exception:
                    if time.monotonic() > deadline:
                        raise TimeoutError("private Vite readiness")
                    time.sleep(.1)
            sim = subprocess.Popen([str(SIM), "http://127.0.0.1:5394/?joatVisual=panel-article", "--automation-port", "9794", "--no-glow"], stdout=sink, stderr=sink)
            processes.append(sim)
            deadline = time.monotonic() + 14
            console = {}
            complete = False
            while time.monotonic() < deadline:
                try:
                    console = json.loads(http("http://127.0.0.1:9794/api/console"))
                    if "[joat audit] complete" in json.dumps(console):
                        complete = True
                        break
                except Exception:
                    pass
                time.sleep(.15)
            time.sleep(.7)
            (OUT / "console.json").write_text(json.dumps(console, indent=2))
            raw = http("http://127.0.0.1:9794/api/screenshot/glasses")
            (OUT / "after-completion.rgba.png").write_bytes(raw)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(OUT / "after-completion.rgba.png"), "-vf", "alphaextract", str(OUT / "after-completion.png")], check=True, timeout=3)
            receipt = {"fixture_complete_marker": complete, "seconds": round(time.monotonic()-started,2), "scope": "temporary instrumented copy; native rebuild results and fixture completion; no Mac connection", "source_edits": "temporary main.ts only"}
            (OUT / "receipt.json").write_text(json.dumps(receipt, indent=2))
            print(json.dumps(receipt))
            for entry in console.get("entries", []):
                if "[joat audit]" in json.dumps(entry):
                    print(json.dumps(entry))
        finally:
            for process in reversed(processes):
                stop(process)
