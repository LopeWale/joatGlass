"""Verify a frozen exact-reader candidate and retain fresh simulator evidence.

Process success is a gate for subsequent image review, never visual acceptance.
The prior compact UI captures and reproduced wire-loss probe are retained.
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
MANIFEST = OUT / "exact-readers-gate-candidate.json"
RECEIPT = OUT / "exact-readers-gate-receipt.json"
PYTHON = str(REPO / ".venv/bin/python")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if RECEIPT.exists() or (OUT / "exact-readers-after").exists():
        raise SystemExit("Preserve existing evidence; scope a new retry.")
    frozen = json.loads(MANIFEST.read_text())
    source = frozen["source_sha256"]
    result = {
        "candidate_manifest_sha256": digest(MANIFEST),
        "source_sha256": source,
        "before_evidence_sha256": frozen["before_evidence_sha256"],
        "checks": [], "source_changed": [], "all_runs_passed": False,
        "visual_review_required": True, "physical_G2": False,
        "real_message_sends": False, "package_or_upload": False,
        "comparison_scope": "Retained exact wire-loss regressions and compact UI screenshots; fresh long mixed-text simulator journeys",
    }

    def changed() -> list[str]:
        return [name for name, expected in source.items()
                if not (REPO / name).is_file() or digest(REPO / name) != expected]

    def save() -> None:
        RECEIPT.write_text(json.dumps(result, indent=2) + "\n")

    checks = [
        ("python", [PYTHON, "-m", "pytest", "-q", "tests/unit/test_reader.py",
                    "tests/unit/test_capabilities.py", "tests/unit/test_panels.py",
                    "tests/unit/test_news.py", "tests/unit/test_search.py", "tests/unit/test_protocol.py"], REPO, 60),
        ("mypy", [PYTHON, "-m", "mypy"], REPO, 60),
        ("hub", ["npm", "test"], REPO / "apps/hub-plugin", 60),
        ("build", ["npm", "run", "build"], REPO / "apps/hub-plugin", 60),
        ("simulator", [PYTHON, str(HERE / "verify_first_wave.py"),
                       "--journey", "exact-readers", "--run-label", "exact-readers"], REPO, 240),
    ]
    save()
    for name, argv, cwd, timeout in checks:
        result["source_changed"] = changed()
        if result["source_changed"]:
            save()
            raise SystemExit("Candidate changed before verification.")
        started = time.monotonic()
        with (OUT / f"exact-readers-gate-{name}.log").open("x") as log:
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
