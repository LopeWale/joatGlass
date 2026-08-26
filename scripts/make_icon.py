#!/usr/bin/env python3
"""Generate the JOAT app icon set (legacy .icns pipeline).

Stopgap per the architecture audit: a flat .icns via iconutil is the only
icon a script-assembled bundle can carry. The Icon Composer / Liquid Glass
icon replaces this when the Xcode app target lands.

Design: deep charcoal rounded square, HUD-green "J" monogram with a
scanline accent — echoes the 576x288 monochrome green HUD identity.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

GREEN = (57, 255, 106, 255)
DIM_GREEN = (57, 255, 106, 60)
CHARCOAL = (16, 20, 18, 255)

SIZES = [16, 32, 128, 256, 512]


def draw_master(px: int = 1024) -> Image.Image:
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = px // 10  # margin so macOS shelf shadowing looks right
    radius = px // 5
    d.rounded_rectangle([m, m, px - m, px - m], radius=radius, fill=CHARCOAL)
    # scanline accent band
    band_y = int(px * 0.62)
    d.rectangle([m, band_y, px - m, band_y + px // 90], fill=DIM_GREEN)
    d.rectangle([m, band_y + px // 45, px - m, band_y + px // 45 + px // 140],
                fill=DIM_GREEN)
    # the J monogram
    font = None
    for cand in ("/System/Library/Fonts/SFNSRounded.ttf",
                 "/System/Library/Fonts/SFNS.ttf",
                 "/System/Library/Fonts/Helvetica.ttc"):
        try:
            font = ImageFont.truetype(cand, int(px * 0.58))
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), "J", font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((px - w) / 2 - bbox[0], (px - h) / 2 - bbox[1] - px * 0.03),
           "J", font=font, fill=GREEN)
    return img


def write_appiconset(target: Path) -> None:
    """Emit an Xcode asset-catalog AppIcon set.

    This is what an Xcode app target consumes; `actool` compiles it into
    Assets.car and the app references it via CFBundleIconName. The legacy
    .icns path below stays for anything building outside Xcode.
    """
    master = draw_master()
    target.mkdir(parents=True, exist_ok=True)
    images = []
    for size in SIZES:
        for scale in (1, 2):
            px = size * scale
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            master.resize((px, px), Image.LANCZOS).save(target / name)
            images.append({
                "size": f"{size}x{size}",
                "idiom": "mac",
                "filename": name,
                "scale": f"{scale}x",
            })
    (target / "Contents.json").write_text(json.dumps(
        {"images": images, "info": {"version": 1, "author": "xcode"}}, indent=2))
    print(f"wrote {target}")


def main(out: str) -> None:
    path = Path(out)
    if path.suffix == ".appiconset":
        write_appiconset(path)
        return
    master = draw_master()
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for size in SIZES:
            master.resize((size, size), Image.LANCZOS).save(
                iconset / f"icon_{size}x{size}.png")
            master.resize((size * 2, size * 2), Image.LANCZOS).save(
                iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", out], check=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "AppIcon.icns")
