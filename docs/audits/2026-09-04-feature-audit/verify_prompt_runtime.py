"""Freeze and verify production prompt composition and CLI completion changes.

This host gate does not claim simulator visuals, live recognition or sends.
The separately authored simulator journey is a subsequent acceptance gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import re
import subprocess
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = HERE / "evidence/remediation"
MANIFEST = OUT / "prompt-runtime-candidate.json"
RECEIPT = OUT / "prompt-runtime-gate-receipt.json"
PYTHON = str(REPO / ".venv/bin/python")

TESTS = [
    "tests/unit/test_audio_router.py", "tests/unit/test_vad.py",
    "tests/unit/test_voice_note_capture.py", "tests/unit/test_session.py",
    "tests/unit/test_prompted_input.py", "tests/unit/test_message_prompts.py",
    "tests/unit/test_message_reply.py", "tests/unit/test_message_tools.py",
    "tests/unit/test_orchestrator.py", "tests/unit/test_prompt_protocol.py",
    "tests/unit/test_cli_reply_engine.py", "tests/unit/test_capabilities.py",
    "tests/unit/test_feature_tools.py", "tests/integration/test_feature_contracts.py",
    "tests/integration/test_prompt_gateway.py", "tests/integration/test_hub_gateway.py",
    "tests/integration/test_prompted_speech.py", "tests/integration/test_prompted_speech_runtime.py",
    "tests/unit/test_agent_terminal.py", "tests/unit/test_openclaw_runtime.py",
    "tests/unit/test_openclaw_terminal.py", "tests/unit/test_openclaw_gateway.py",
    "tests/unit/test_protocol.py", "tests/integration/test_terminal_opening.py",
    "tests/integration/test_message_ui.py", "tests/integration/test_message_reply_entry.py",
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", default="prompt-runtime")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--include-hub", action="store_true")
    args = parser.parse_args()
    if re.fullmatch(r"[a-z0-9-]+", args.run_label) is None or not 1 <= args.timeout <= 300:
        parser.error("Use a simple run label and a timeout between 1 and 300 seconds.")
    manifest = OUT / f"{args.run_label}-candidate.json"
    receipt = OUT / f"{args.run_label}-gate-receipt.json"
    if receipt.exists():
        raise SystemExit("Retain prior evidence; another run needs a justified new scope.")
    frozen = json.loads(manifest.read_text())
    source = frozen["source_sha256"]
    result = {
        "candidate_manifest_sha256": digest(manifest), "source_sha256": source,
        "check_timeout_seconds": args.timeout,
        "scope": "Production Message/Brainstorm/Terminal prompted speech, recording boundary, local Terminal context lifecycle, and Codex completed-answer host latency",
        "checks": [], "source_changed": [], "simulator_visuals": "pending separate gate",
        "physical_G2": False, "real_message_sends": False, "real_stt_accuracy": False,
        "legacy_messages_ui": "controlled entry, exact prompted input and outcome gate; simulator acceptance pending", "complete_main_startup_verified": False,
        "terminal_first_use_setup": "controlled composition gate; real Gateway and simulator acceptance still required",
    }

    def changed() -> list[str]:
        return [name for name, expected in source.items()
                if not (REPO / name).is_file() or digest(REPO / name) != expected]

    def save() -> None:
        receipt.write_text(json.dumps(result, indent=2) + "\n")

    checks = [
        ("python", [PYTHON, "-m", "pytest", "-vv", *TESTS, "--durations=15",
                    "-o", "junit_family=xunit1", "--junitxml=" + str(OUT / f"{args.run_label}-gate.xml")], REPO),
        ("mypy", [PYTHON, "-m", "mypy"], REPO),
    ]
    if args.include_hub:
        checks.extend([
            ("hub-tests", ["npm", "test", "--", "--run"], REPO / "apps/hub-plugin"),
            ("hub-build", ["npm", "run", "build"], REPO / "apps/hub-plugin"),
        ])
    for name, argv, cwd in checks:
        result["source_changed"] = changed()
        save()
        if result["source_changed"]:
            raise SystemExit("Source changed before verification.")
        started = time.monotonic()
        with (OUT / f"{args.run_label}-gate-{name}.log").open("x") as log:
            child = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            try:
                status: int | str = child.wait(timeout=args.timeout)
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
