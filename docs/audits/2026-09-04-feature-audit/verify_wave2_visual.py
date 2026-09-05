"""Continue the unexecuted Wave 2 simulator gate without repeating source tests.

The only changes from that source gate are the guarded test access and the
reader evaluator. Each comparison uses a fresh disposable simulator host.
The historical failed receipt and logs remain immutable.
"""

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
OUT = HERE / "evidence/remediation"
PYTHON = str(REPO / ".venv/bin/python")
RECEIPT = OUT / "wave2-visual-receipt.json"
if RECEIPT.exists():
    raise SystemExit("Visual receipt already exists; preserve it and scope a retry separately.")

prior = json.loads((OUT / "wave2-receipt.json").read_text())
allowed_changes = {
    "apps/hub-plugin/src/CapabilityMenu.test.ts",
    str((HERE / "verify_first_wave.py").relative_to(REPO)),
}
delta = [path for path, digest in prior["source_sha256"].items()
         if hashlib.sha256((REPO / path).read_bytes()).hexdigest() != digest]
if set(delta) - allowed_changes:
    raise SystemExit(f"Unreviewed change from passing source gates: {delta}")
passed = {check["name"] for check in prior["checks"] if check["exit_code"] == 0}
if not {"python", "mypy", "hub"} <= passed or prior["source_changed"]:
    raise SystemExit("The earlier source gates are not reusable.")

evidence_names = (
    "wave2-receipt.json", "wave2-python.log", "wave2-mypy.log", "wave2-hub.log",
    "wave2-build-corrected.log", "wave2-menu-corrected.log",
)
evidence = {name: hashlib.sha256((OUT / name).read_bytes()).hexdigest()
            for name in evidence_names}
if "built in" not in (OUT / "wave2-build-corrected.log").read_text():
    raise SystemExit("Corrected build evidence is missing.")
if "88 passed" not in (OUT / "wave2-menu-corrected.log").read_text():
    raise SystemExit("Corrected menu test evidence is missing.")

files = [REPO / path for path in prior["source_sha256"]]
files += [REPO / path for path in (
    "apps/hub-plugin/app.json", "apps/hub-plugin/index.html",
    "apps/hub-plugin/tsconfig.json", "apps/hub-plugin/package.json",
)]
files.append(Path(__file__))
source = {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
          for path in sorted(set(files))}
result = {
    "source_sha256": source,
    "reused_source_evidence_sha256": evidence,
    "reviewed_delta_from_source_gate": delta,
    "additional_config_hashes_frozen_at_visual_gate": True,
    "checks": [], "physical_G2": False, "real_message_sends": False,
    "visual_review_required": True, "back_input_source_normalized": True,
    "multi_feature_sequence_accepted": False,
    "recording_transcript_visual_accepted": False,
    "comparison": "same data/actions; one fresh simulator per individual journey",
}


def changed() -> list[str]:
    return [path for path, digest in source.items()
            if hashlib.sha256((REPO / path).read_bytes()).hexdigest() != digest]


def save() -> None:
    RECEIPT.write_text(json.dumps(result, indent=2) + "\n")


def run(journey: str, baseline: bool) -> bool:
    result["source_changed"] = changed()
    if result["source_changed"]:
        save()
        raise SystemExit("Candidate changed during verification; receipt retained.")
    side = "before" if baseline else "after"
    name = f"{journey}-{side}"
    label = f"wave2-visual-{journey}"
    if (OUT / f"{label}-{side}").exists():
        raise SystemExit(f"Existing journey evidence: {label}-{side}")
    argv = [PYTHON, str(HERE / "verify_first_wave.py"), "--journey", journey,
            "--run-label", label]
    if baseline:
        argv.append("--baseline")
    started = time.monotonic()
    with (OUT / f"wave2-visual-{name}.log").open("x") as log:
        child = subprocess.Popen(argv, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            status: int | str = child.wait(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=3)
            status = "timeout"
    check = {"name": name, "exit_code": status,
             "seconds": round(time.monotonic() - started, 3)}
    result["checks"].append(check)
    result["source_changed"] = changed()
    save()
    print(json.dumps(check), flush=True)
    return status == 0 and not result["source_changed"]


save()
all_passed = True
for journey in ("quicknotes", "messages", "news", "note-reader"):
    for baseline in (True, False):
        # A failing journey must not erase the other independent comparisons.
        all_passed = run(journey, baseline) and all_passed
result["all_runs_passed"] = all_passed
save()
raise SystemExit(0 if all_passed else 1)
