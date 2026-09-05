"""Retry two simulator entries after a verified fixture-only correction.

Reuse the passing Python/mypy/Hub/build checks and News journey. Preserve the
failed first run; this retry cannot turn emoji raster fidelity into acceptance.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = HERE / "evidence/remediation"
MANIFEST = OUT / "reader-entry-retry-candidate.json"
RECEIPT = OUT / "reader-entry-retry-receipt.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if RECEIPT.exists() or (OUT / "reader-entry-retry-after").exists():
        raise SystemExit("Preserve existing evidence; scope another retry.")
    frozen = json.loads(MANIFEST.read_text())
    source = frozen["source_sha256"]
    prior_path = OUT / "exact-readers-gate-receipt.json"
    prior = json.loads(prior_path.read_text())
    wanted = {"python", "mypy", "hub", "build"}
    passed = {c["name"] for c in prior["checks"] if c["exit_code"] == 0}
    if not wanted <= passed or prior["source_changed"]:
        raise SystemExit("Prior source gates are not reusable.")
    result = {
        "candidate_manifest_sha256": digest(MANIFEST),
        "prior_gate_sha256": digest(prior_path),
        "reused_checks": sorted(wanted),
        "source_sha256": source, "source_changed": [],
        "scope": "Fixture-only voice-prefixed entry fix; Messages and Notes only",
        "visual_review_required": True, "emoji_raster_fidelity": "unverified",
        "physical_G2": False, "real_message_sends": False,
    }

    def changed() -> list[str]:
        return [name for name, expected in source.items()
                if not (REPO / name).is_file() or digest(REPO / name) != expected]

    def save() -> None:
        RECEIPT.write_text(json.dumps(result, indent=2) + "\n")

    result["source_changed"] = changed()
    save()
    if result["source_changed"]:
        raise SystemExit("Candidate changed before verification.")
    started = time.monotonic()
    with (OUT / "reader-entry-retry.log").open("x") as log:
        process = subprocess.run([
            str(REPO / ".venv/bin/python"), str(HERE / "verify_first_wave.py"),
            "--journey", "exact-readers", "--run-label", "reader-entry-retry",
            "--exact-feature", "message", "--exact-feature", "note",
        ], cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
    result["exit_code"] = process.returncode
    result["seconds"] = round(time.monotonic() - started, 3)
    result["source_changed"] = changed()
    save()
    print(json.dumps({k: v for k, v in result.items() if k != "source_sha256"}), flush=True)
    if process.returncode or result["source_changed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
