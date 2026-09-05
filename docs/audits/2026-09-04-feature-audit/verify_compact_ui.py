"""Verify the frozen compact JOAT UI using current source and three simulator journeys.

The previous News capture is retained as the before receipt. The fresh after
journey uses the same fixtures and real simulator gestures. Human image review
is required after this runner completes; process success is not visual proof.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = HERE / "evidence/remediation"
RECEIPT = OUT / "compact-ui-gate-receipt.json"
MANIFEST = OUT / "compact-ui-gate-candidate.json"
PYTHON = str(REPO / ".venv/bin/python")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if RECEIPT.exists() or any((OUT / ("compact-" + name + "-after")).exists() for name in ("news", "messages", "reader")):
        raise SystemExit("Existing evidence must be preserved; scope a new retry.")
    frozen = json.loads(MANIFEST.read_text())
    source = frozen["source_sha256"]
    result = {
        "candidate_manifest_sha256": digest(MANIFEST),
        "source_sha256": source,
        "before_evidence_sha256": frozen["before_evidence_sha256"],
        "checks": [], "source_changed": [],
        "visual_review_required": True,
        "physical_G2": False, "live_prompted_speech_wired": False, "latest_intended_design_acceptance": "pending wearer and visual review",
        "real_message_sends": False,
        "news_comparison": "Reused immediate pre-repair News captures; same fixture and gestures in fresh after run",
    }

    def changed() -> list[str]:
        return [name for name, expected in source.items()
                if not (REPO / name).is_file() or digest(REPO / name) != expected]

    def save() -> None:
        RECEIPT.write_text(json.dumps(result, indent=2) + "\n")

    checks = [
        ("python", [PYTHON, "-m", "pytest", "-q", "tests/unit/test_capabilities.py",
                    "tests/unit/test_session.py", "tests/unit/test_message_prompts.py",
                    "tests/unit/test_prompted_input.py", "tests/integration/test_prompted_speech.py"], REPO, 180),
        ("mypy", [PYTHON, "-m", "mypy"], REPO, 60),
        ("hub", ["npm", "test"], REPO / "apps/hub-plugin", 60),
        ("build", ["npm", "run", "build"], REPO / "apps/hub-plugin", 60),
    ]
    checks.extend((name, [PYTHON, str(HERE / "verify_first_wave.py"), "--journey", journey,
                         "--run-label", "compact-" + name], REPO, 120)
                  for name, journey in [("news", "news"), ("messages", "messages"), ("reader", "note-reader")])
    checks.append(("pixel-measurements", [PYTHON, str(HERE / "measure_compact_ui.py")], REPO, 30))
    save()
    for name, argv, cwd, timeout in checks:
        result["source_changed"] = changed()
        if result["source_changed"]:
            save()
            raise SystemExit("Candidate changed before verification.")
        started = time.monotonic()
        with (OUT / f"compact-ui-gate-{name}.log").open("x") as log:
            child = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            try:
                status: int | str = child.wait(timeout=timeout)
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
        if status != 0 or result["source_changed"]:
            raise SystemExit(1)
    result["all_runs_passed"] = True
    save()


if __name__ == "__main__":
    main()
