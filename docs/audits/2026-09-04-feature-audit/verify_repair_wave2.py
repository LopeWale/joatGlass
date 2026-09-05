"""Freeze one repair candidate, check it, and capture independent UI comparisons.

Each simulator journey starts with a fresh disposable host. This diagnoses
individual repairs; it does not close the unresolved multi-feature sequence.
All fixtures are synthetic and only their own child processes are stopped.
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
RECEIPT = OUT / "wave2-receipt.json"
if RECEIPT.exists():
    raise SystemExit("Wave 2 receipt already exists; preserve it and scope any retry separately.")

files = [p for base in (REPO / "apps/mac/joat", REPO / "apps/hub-plugin/src")
         for p in base.rglob("*") if p.suffix in (".py", ".ts")]
files += [REPO / path for path in (
    "tests/unit/test_capabilities.py", "tests/unit/test_imessage.py",
    "tests/unit/test_message_reply.py", "tests/unit/test_feature_tools.py",
    "tests/unit/test_message_tools.py", "tests/integration/test_feature_contracts.py",
    "scripts/simulator_brainstorm_action_flow_acceptance.py",
    "apps/hub-plugin/package-lock.json", "pyproject.toml",
)]
files += [Path(__file__), HERE / "verify_first_wave.py"]
source = {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
          for path in sorted(set(files))}
result = {
    "source_sha256": source, "checks": [], "physical_G2": False,
    "visual_review_required": True, "real_message_sends": False,
    "back_input_source_normalized": True,
    "multi_feature_sequence_accepted": False,
    "comparison": "same data/actions; one fresh simulator per individual journey",
}


def changed() -> list[str]:
    return [path for path, digest in source.items()
            if hashlib.sha256((REPO / path).read_bytes()).hexdigest() != digest]


def run(name: str, argv: list[str], *, cwd: Path = REPO, timeout: int = 120) -> bool:
    if changed():
        result["source_changed"] = changed()
        RECEIPT.write_text(json.dumps(result, indent=2) + "\n")
        raise SystemExit("Candidate changed during verification; receipt retained.")
    started = time.monotonic()
    with (OUT / f"wave2-{name}.log").open("w") as log:
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
    check = {"name": name, "exit_code": status, "seconds": round(time.monotonic() - started, 3)}
    result["checks"].append(check)
    result["source_changed"] = changed()
    RECEIPT.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(check), flush=True)
    return status == 0 and not result["source_changed"]


gates = [
    ("python", [PYTHON, "-m", "pytest", "tests/unit/test_capabilities.py",
        "tests/unit/test_imessage.py", "tests/unit/test_message_reply.py",
        "tests/unit/test_feature_tools.py", "tests/unit/test_message_tools.py",
        "tests/integration/test_feature_contracts.py", "-q"], REPO),
    ("mypy", [PYTHON, "-m", "mypy", "apps/mac/joat"], REPO),
    ("hub", ["npm", "test", "--", "--run"], REPO / "apps/hub-plugin"),
    ("build", ["npm", "run", "build"], REPO / "apps/hub-plugin"),
]
for name, argv, cwd in gates:
    if not run(name, argv, cwd=cwd):
        raise SystemExit(1)

all_passed = True
for journey in ("quicknotes", "messages", "news", "note-reader"):
    for baseline in (True, False):
        side = "before" if baseline else "after"
        argv = [PYTHON, str(HERE / "verify_first_wave.py"), "--journey", journey,
                "--run-label", f"wave2-{journey}"]
        if baseline:
            argv.append("--baseline")
        # A failed journey must not erase independent comparisons. The final
        # job remains failed until every required run has a successful exit.
        all_passed = run(f"{journey}-{side}", argv) and all_passed
result["all_runs_passed"] = all_passed
RECEIPT.write_text(json.dumps(result, indent=2) + "\n")
raise SystemExit(0 if all_passed else 1)
