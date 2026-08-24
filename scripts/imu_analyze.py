#!/usr/bin/env python3
"""Pick a nod threshold from captured IMU traces.

    python3 scripts/imu_analyze.py traces/*.csv

Replays each trace through the same sliding-window swing calculation the
plugin's ImuMonitor uses, then reports the swing distribution per gesture and
suggests a threshold that separates nods from everything else.

The number that matters is the SEPARATION between the nod p05 and the loudest
non-nod p95. If they overlap, no threshold works and the nod is not a usable
confirmer — say so and rely on the temple tap rather than shipping a gate that
fires while you walk.

Stdlib only. No numpy — this is a few hundred samples, not a dataset.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

WINDOW_MS = 1200  # must match ImuMonitor's windowMs


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Fine at these sample counts."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(round(pct / 100 * len(ordered) + 0.5)) - 1, len(ordered) - 1)
    return ordered[max(idx, 0)]


def swings(rows: list[dict], window_ms: int = WINDOW_MS) -> list[float]:
    """Max-minus-min of .y over a sliding time window — ImuMonitor's metric."""
    out: list[float] = []
    window: list[tuple[float, float]] = []  # (at, y)
    for row in rows:
        try:
            at, y = float(row["at"]), float(row["y"])
        except (TypeError, ValueError):
            continue
        window.append((at, y))
        window = [(t, v) for t, v in window if at - t <= window_ms]
        ys = [v for _, v in window]
        out.append(max(ys) - min(ys))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csvs", nargs="+", type=Path)
    parser.add_argument("--window-ms", type=int, default=WINDOW_MS)
    args = parser.parse_args()

    by_label: dict[str, list[float]] = defaultdict(list)
    for path in args.csvs:
        with path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            print(f"{path}: empty, skipped")
            continue
        label = rows[0].get("label", path.stem)
        by_label[label].extend(swings(rows, args.window_ms))

    if not by_label:
        print("no usable traces")
        return 1

    print(f"{'gesture':<10} {'n':>6} {'p05':>8} {'p50':>8} {'p95':>8} {'max':>8}")
    for label, vals in sorted(by_label.items()):
        print(
            f"{label:<10} {len(vals):>6} {percentile(vals, 5):>8.3f} "
            f"{percentile(vals, 50):>8.3f} {percentile(vals, 95):>8.3f} {max(vals):>8.3f}"
        )

    if "nod" not in by_label:
        print("\nNo 'nod' trace — capture one to get a threshold suggestion.")
        return 0

    nod_floor = percentile(by_label["nod"], 5)
    others = {k: v for k, v in by_label.items() if k != "nod"}
    if not others:
        print("\nOnly nods captured. Capture 'walk' and 'still' too — a threshold")
        print("that separates nods from nothing is not a threshold.")
        return 0

    noisiest, noise_ceiling = max(
        ((k, percentile(v, 95)) for k, v in others.items()), key=lambda kv: kv[1]
    )

    print(f"\nnod p05          {nod_floor:.3f}")
    print(f"loudest non-nod  {noise_ceiling:.3f}  ({noisiest} p95)")

    if nod_floor <= noise_ceiling:
        print("\nOVERLAP — no threshold separates these cleanly.")
        print("The nod is not a reliable confirmer with this metric. Use the")
        print("temple tap, and consider a better feature (zero-crossing count,")
        print("or a swing-and-return check) before trusting a nod gate.")
        return 2

    suggested = (nod_floor + noise_ceiling) / 2
    print(f"\nSuggested threshold: {suggested:.3f}")
    print(f"  margin below nods:      {nod_floor - suggested:+.3f}")
    print(f"  margin above {noisiest:<10} {suggested - noise_ceiling:+.3f}")
    print(f"\nSet in main.ts:  new ImuMonitor(client, link, {suggested:.2f}, {args.window_ms})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
