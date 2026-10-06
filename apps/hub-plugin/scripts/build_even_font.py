#!/usr/bin/env python3
"""Build src/assets/even-font.{png,json}: the firmware font, cut from the
official simulator, so JOAT can set Even's own letterforms in image
containers at a size the firmware will not (EvenFont.ts).

The G2 firmware ships one LVGL font ("evenroster", with a Greek/Cyrillic
fallback) and gives plugins no font or size choice. @evenrealities/pretext
carries its advances but not its shapes. The official simulator renders that
font exactly as the firmware does (the same 16-level anti-aliasing), so this
script has it draw every glyph (tools/glyph-sheet.html) and cuts each one out
at the position pretext gives it.

Usage (from apps/hub-plugin, with `npx vite --port 5199` running):
    python3 scripts/build_even_font.py --simulator /path/to/evenhub-simulator \
        --base http://127.0.0.1:5199

Needs Pillow, ffmpeg-free; xvfb-run on Linux. The result is Even's font: keep
it inside this private plugin, never redistribute it on its own.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import unicodedata
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PRETEXT = ROOT / "node_modules/@evenrealities/pretext/dist/font_measure.js"
CHARSET = ROOT / "tools/even-font-charset.json"
OUT_PNG = ROOT / "src/assets/even-font.png"
OUT_JSON = ROOT / "src/assets/even-font.json"
LINE = 27
PAD = 4  # the sheet separates glyphs by two spaces (10 px)
ATLAS_WIDTH = 1024


def charset() -> list[int]:
    source = PRETEXT.read_text()
    starts = [m.start() for m in re.finditer(r'"name":\s*"', source)]
    points: list[int] = []
    for index, start in enumerate(starts):
        chunk = source[start: starts[index + 1] if index + 1 < len(starts) else len(source)]
        name = re.match(r'"name":\s*"([^"]+)"', chunk)
        if not name or name.group(1) not in ("evenroster", "evenroster_crylgrek"):
            continue
        glyphs = re.search(r'"glyphs":\s*\{([^}]*)\}', chunk, re.S)
        if glyphs:
            points += [int(k) for k in re.findall(r'"(\d+)":', glyphs.group(1))]
    # Latin-1 and the punctuation and symbols an interface uses. Latin
    # Extended-A is left out: the simulator drops a shifting set of those
    # glyphs from pass to pass, so no sheet of them ever comes out whole. The Greek
    # and Cyrillic fallback is left out: the simulator drops many of those
    # glyphs, and a dropped glyph shifts every later one on its sheet line.
    # Anything outside the set is drawn by the canvas fallback instead.
    wanted = (
        (0x21, 0x7E), (0xA1, 0xFF), (0x2010, 0x203A), (0x20A0, 0x20BF), (0x2100, 0x2153),
        (0x2190, 0x21FF), (0x2200, 0x22FF), (0x2500, 0x25FF), (0x2600, 0x26FF),
    )
    return sorted({p for p in points if any(lo <= p <= hi for lo, hi in wanted)})


def http(url: str, data: bytes | None = None) -> bytes:
    with urllib.request.urlopen(url, data=data, timeout=15) as response:
        return response.read()


def capture(simulator: str, base: str, page: int, port: int, work: Path) -> tuple[Image.Image, dict]:
    process = subprocess.Popen(
        ["xvfb-run", "-a", simulator, f"{base}/tools/glyph-sheet.html?page={page}",
         "--automation-port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        automation = f"http://127.0.0.1:{port}"
        deadline = time.time() + 40
        placed = None
        while time.time() < deadline:
            try:
                entries = json.loads(http(f"{automation}/api/console"))["entries"]
            except Exception:
                time.sleep(0.5)
                continue
            messages = [e["message"] for e in entries]
            for message in messages:
                if message.startswith("GLYPHS "):
                    placed = json.loads(message[7:])
            if placed and any("SHEET_READY" in m for m in messages):
                break
            time.sleep(0.5)
        if placed is None:
            raise RuntimeError(f"page {page}: the sheet never reported its glyphs")
        time.sleep(2)
        shot = work / f"sheet-{page}.png"
        shot.write_bytes(http(f"{automation}/api/screenshot/glasses"))
        return Image.open(shot).split()[3], placed
    finally:
        subprocess.run(["pkill", "-f", f"automation-port {port}"], check=False)
        process.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator", required=True)
    parser.add_argument("--base", default="http://127.0.0.1:5199")
    parser.add_argument("--port", type=int, default=9930)
    parser.add_argument("--work", type=Path, default=Path("/tmp/even-font"))
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)

    # The firmware drops a glyph it lacks, which shifts the rest of that
    # sheet line. A blank cell marks such a glyph: leave it out of the
    # charset and draw the sheets again until every line is whole.
    missing: set[int] = set()
    for attempt in range(4):
        CHARSET.write_text(json.dumps([p for p in charset() if p not in missing]))
        cells: list[tuple[int, Image.Image, float]] = []
        blank_found: set[int] = set()
        page, pages = 0, 1
        while page < pages:
            alpha, placed = capture(args.simulator, args.base, page, args.port + 10 * attempt + page, args.work)
            pages = placed["pages"]
            by_line: dict[int, list] = {}
            for glyph in placed["placed"]:
                by_line.setdefault(glyph["line"], []).append(glyph)
            for line, glyphs in sorted(by_line.items()):
                y = line * LINE
                crops = []
                for glyph in glyphs:
                    x = round(glyph["x"])
                    crops.append((glyph, alpha.crop((x - PAD, y, x + round(glyph["advance"]) + PAD, y + LINE))))
                # Spaces and invisible format characters are blank by design.
                blank = {g["cp"] for g, cell in crops if cell.getbbox() is None
                         and unicodedata.category(chr(g["cp"])) not in ("Zs", "Cf")}
                if blank:
                    blank_found |= blank
                    continue
                cells.extend((g["cp"], cell, g["advance"]) for g, cell in crops)
            page += 1
        if not blank_found:
            break
        print(f"pass {attempt + 1}: {len(blank_found)} glyphs the firmware lacks; drawing again without them:"
              f" {' '.join(hex(c) for c in sorted(blank_found))}", flush=True)
        missing |= blank_found
    else:
        raise RuntimeError("sheets still carry blank glyphs after four passes")

    rows: list[list[tuple[int, Image.Image, float]]] = [[]]
    used = 0
    for item in cells:
        if used + item[1].width > ATLAS_WIDTH:
            rows.append([])
            used = 0
        rows[-1].append(item)
        used += item[1].width
    atlas = Image.new("LA", (ATLAS_WIDTH, LINE * len(rows)), (255, 0))
    glyphs: dict[str, list[float]] = {}
    for row_index, row in enumerate(rows):
        x = 0
        for codepoint, cell, advance in row:
            atlas.paste(Image.merge("LA", (Image.new("L", cell.size, 255), cell)), (x, row_index * LINE))
            glyphs[str(codepoint)] = [x, row_index * LINE, cell.width, advance]
            x += cell.width
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(OUT_PNG, optimize=True)
    OUT_JSON.write_text(json.dumps({"lineHeight": LINE, "pad": PAD, "glyphs": glyphs}, separators=(",", ":")))
    print(f"{len(glyphs)} glyphs from {pages} pages -> {OUT_PNG.name} {atlas.size}, {OUT_PNG.stat().st_size} bytes;"
          f" {len(missing)} the firmware lacks left out")


if __name__ == "__main__":
    main()
