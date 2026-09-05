"""Read unmodified simulator PNGs and report lit-pixel extents; never score design."""
from __future__ import annotations
import json
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent / "evidence/remediation"

def bounds(path: Path, region: tuple[int, int, int, int]) -> dict:
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path),
        "-f", "rawvideo", "-pix_fmt", "gray", "-"], timeout=5)
    if len(raw) != 576 * 288:
        raise ValueError("Expected an exact 576x288 simulator image")
    left, top, right, bottom = region
    points = [(x, y) for y in range(top, bottom) for x in range(left, right)
              if raw[y * 576 + x] >= 40]
    if not points:
        return {"lit_pixels": 0}
    x0, y0 = min(x for x, _ in points), min(y for _, y in points)
    x1, y1 = max(x for x, _ in points), max(y for _, y in points)
    return {"lit_pixels": len(points), "left": x0, "top": y0,
            "right": x1, "bottom": y1, "width": x1-x0+1, "height": y1-y0+1}

def main() -> None:
    before = BASE / "wave2-visual-note-reader-after/reader-page-1.png"
    after = BASE / "compact-reader-after/reader-page-1.png"
    result = {"basis": "Exact 576x288 PNGs; grayscale threshold 40; identical synthetic Note source. Bounds measure occupied pixels, not readability or physical-G2 delivery.",
              "before": str(before.relative_to(BASE)), "after": str(after.relative_to(BASE)),
              "banner_label": {"before": bounds(before, (95, 2, 300, 35)), "after": bounds(after, (95, 2, 300, 35))},
              "reader_body": {"before": bounds(before, (0, 78, 576, 245)), "after": bounds(after, (0, 78, 576, 245))}}
    for name in ("compact-news", "compact-messages", "compact-reader"):
        receipt = json.loads((BASE / (name + "-after") / "receipt.json").read_text())
        result[name] = {"metrics": receipt["metrics"], "baseline": receipt["baseline"],
                        "captured_frames": len(receipt["frames"])}
    target = BASE / "compact-ui-pixel-measurements.json"
    with target.open("x") as out:
        json.dump(result, out, indent=2)
        out.write("\n")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
