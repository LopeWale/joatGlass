#!/usr/bin/env python3
"""Sign the two Messages shortcuts so an iPhone can import them, optionally
for a JOAT Brain signed by another Apple team or with another bundle ID.

macOS only: it uses Apple's `shortcuts sign`. Sources are the unsigned
shortcuts in source/ (XML plists); the signed files land next to this script.

    python3 make_shortcuts.py                       # re-sign as they are
    python3 make_shortcuts.py --team ABCDE12345     # your own Apple team
    python3 make_shortcuts.py --team ABCDE12345 --bundle com.you.joatbrain
"""
import argparse
import os
import plistlib
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEAM_PLACEHOLDER, BUNDLE = "YOURTEAMID", "com.joat.brain"


def retarget(value, team, bundle):
    """Every reference to JOAT Brain, pointed at the given team and bundle."""
    if isinstance(value, dict):
        return {k: retarget(v, team, bundle) for k, v in value.items()}
    if isinstance(value, list):
        return [retarget(v, team, bundle) for v in value]
    if value == TEAM_PLACEHOLDER:
        return team
    if isinstance(value, str) and (value == BUNDLE or value.startswith(BUNDLE + ".")):
        return bundle + value[len(BUNDLE):]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--team",
        default=os.environ.get("JOAT_TEAM_ID") or TEAM_PLACEHOLDER,
        help="Apple team ID that signs your JOAT Brain (or set JOAT_TEAM_ID)",
    )
    parser.add_argument("--bundle", default=BUNDLE, help="your JOAT Brain bundle ID")
    args = parser.parse_args()
    sources = sorted((HERE / "source").glob("*.plist"))
    if not sources:
        raise SystemExit("no shortcuts in source/")
    with tempfile.TemporaryDirectory() as tmp:
        for source in sources:
            shortcut = retarget(plistlib.loads(source.read_bytes()), args.team, args.bundle)
            unsigned = Path(tmp) / f"{source.stem}.shortcut"  # `shortcuts sign` wants .shortcut
            unsigned.write_bytes(plistlib.dumps(shortcut, fmt=plistlib.FMT_BINARY))
            signed = HERE / f"{source.stem}.shortcut"
            subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned),
                            "--output", str(signed)], check=True)
            print(f"signed {signed.name}")


if __name__ == "__main__":
    main()
