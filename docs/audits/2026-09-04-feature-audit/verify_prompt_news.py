"""Verify a frozen prompted-speech candidate and the News Back visual repair.

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
RECEIPT = OUT / "prompt-news-receipt.json"
MANIFEST = OUT / "prompt-news-candidate.json"
PYTHON = str(REPO / ".venv/bin/python")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if RECEIPT.exists() or (OUT / "prompt-news-after").exists():
        raise SystemExit("Existing evidence must be preserved; scope a new retry.")
    frozen = json.loads(MANIFEST.read_text())
    source = frozen["source_sha256"]
    result = {
        "candidate_manifest_sha256": digest(MANIFEST),
        "source_sha256": source,
        "before_evidence_sha256": frozen["before_evidence_sha256"],
        "checks": [], "source_changed": [],
        "visual_review_required": True,
        "physical_G2": False, "live_prompted_speech_wired": False,
        "real_message_sends": False,
        "news_comparison": "Reused immediate pre-repair News captures; same fixture and gestures in fresh after run",
    }

    def changed() -> list[str]:
        return [name for name, expected in source.items()
                if not (REPO / name).is_file() or digest(REPO / name) != expected]

    def save() -> None:
        RECEIPT.write_text(json.dumps(result, indent=2) + "\n")

    checks = [
        ("python", [PYTHON, "-m", "pytest", "-q",
                    "tests/unit/test_prompted_input.py", "tests/unit/test_message_prompts.py",
                    "tests/unit/test_orchestrator.py", "tests/unit/test_vad.py",
                    "tests/unit/test_message_reply.py", "tests/unit/test_message_tools.py",
                    "tests/unit/test_feature_tools.py", "tests/unit/test_capabilities.py",
                    "tests/unit/test_imessage.py",
                    "tests/integration/test_feature_contracts.py",
                    "tests/integration/test_prompted_speech.py"], REPO, 60),
        ("mypy", [PYTHON, "-m", "mypy"], REPO, 60),
        ("hub", ["npm", "test"], REPO / "apps/hub-plugin", 60),
        ("build", ["npm", "run", "build"], REPO / "apps/hub-plugin", 60),
        ("news", [PYTHON, str(HERE / "verify_first_wave.py"), "--journey", "news",
                  "--run-label", "prompt-news"], REPO, 120),
    ]
    save()
    for name, argv, cwd, timeout in checks:
        result["source_changed"] = changed()
        if result["source_changed"]:
            save()
            raise SystemExit("Candidate changed before verification.")
        started = time.monotonic()
        with (OUT / f"prompt-news-{name}.log").open("x") as log:
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
