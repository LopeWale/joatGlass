"""Parent-owned finite verification for the first remediation slice."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "evidence/remediation"
PYTHON = str(REPO / ".venv/bin/python")
commands = [
    ("mac-final", [PYTHON, "-m", "pytest", "tests/unit/test_capabilities.py", "tests/unit/test_imessage.py", "-q"], REPO),
    ("mypy-final", [PYTHON, "-m", "mypy"], REPO),
    ("hub-final", ["npm", "test"], REPO / "apps/hub-plugin"),
    ("hub-build", ["npm", "run", "build"], REPO / "apps/hub-plugin"),
    ("simulator-before", [PYTHON, str(Path(__file__).with_name("verify_first_wave.py")), "--baseline"], REPO),
    ("simulator-after", [PYTHON, str(Path(__file__).with_name("verify_first_wave.py"))], REPO),
]
source = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
          for base in (REPO / "apps/mac/joat", REPO / "apps/hub-plugin/src")
          for p in sorted(base.rglob("*")) if p.suffix in (".py", ".ts")}
result = {"source_sha256": source, "checks": [], "physical_G2": False,
          "visual_review_required": True, "source_mutation": "none by this runner"}
for name, argv, cwd in commands:
    started = time.monotonic()
    with (OUT / f"{name}.log").open("w") as log:
        try:
            run = subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, timeout=180)
            status = run.returncode
        except subprocess.TimeoutExpired:
            status = "timeout"
    check = {"name": name, "exit_code": status, "seconds": round(time.monotonic()-started,2)}
    result["checks"].append(check)
    print(json.dumps(check), flush=True)
    (OUT / "batch-receipt.json").write_text(json.dumps(result, indent=2)+"\n")
    if status != 0:
        raise SystemExit(1)
result["source_changed"] = [p for p, h in source.items() if hashlib.sha256((REPO/p).read_bytes()).hexdigest() != h]
(OUT / "batch-receipt.json").write_text(json.dumps(result, indent=2)+"\n")
if result["source_changed"]:
    raise SystemExit(1)
