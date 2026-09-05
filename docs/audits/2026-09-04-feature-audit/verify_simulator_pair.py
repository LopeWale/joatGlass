"""Only the outstanding, matched simulator journeys; keep earlier logs intact."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "evidence/remediation"
PYTHON = str(REPO / ".venv/bin/python")
source = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
          for base in (REPO / "apps/mac/joat", REPO / "apps/hub-plugin/src")
          for p in sorted(base.rglob("*")) if p.suffix in (".py", ".ts")}
for relative in ("tests/unit/test_capabilities.py", "scripts/simulator_brainstorm_action_flow_acceptance.py"):
    source[relative] = hashlib.sha256((REPO / relative).read_bytes()).hexdigest()
previous = json.loads((OUT / "remaining-receipt.json").read_text())
changed_production = [p for p, h in previous["source_sha256"].items()
                      if source.get(p) != h and not p.endswith(".test.ts")]
if changed_production:
    raise SystemExit(f"Unexpected production delta since completed Hub run: {changed_production}")
result = {"source_sha256": source, "checks": [], "physical_G2": False,
          "visual_review_required": True, "back_input_source_normalized": True,
          "earlier_checks": ["batch-receipt.json", "remaining-receipt.json",
                             "menu-cancellation-after.log", "hub-menu-build-final.log"]}
for name, arguments in (("simulator-before", ["--baseline"]), ("simulator-after", [])):
    started = time.monotonic()
    with (OUT / f"{name}.log").open("w") as log:
        try:
            run = subprocess.Popen([PYTHON, str(Path(__file__).with_name("verify_first_wave.py")), *arguments],
                cwd=REPO, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            status = run.wait(timeout=180)
        except subprocess.TimeoutExpired:
            # This process group contains only this disposable journey and its
            # Vite/native children. Do not leave a simulator owning our port.
            os.killpg(run.pid, signal.SIGTERM)
            try:
                run.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(run.pid, signal.SIGKILL)
                run.wait(timeout=3)
            status = "timeout"
    check = {"name": name, "exit_code": status, "seconds": round(time.monotonic()-started, 2)}
    result["checks"].append(check)
    result["source_changed"] = [p for p, h in source.items()
        if hashlib.sha256((REPO/p).read_bytes()).hexdigest() != h]
    print(json.dumps(check), flush=True)
    (OUT / "simulator-pair-receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    if status != 0 or result["source_changed"]:
        raise SystemExit(1)
