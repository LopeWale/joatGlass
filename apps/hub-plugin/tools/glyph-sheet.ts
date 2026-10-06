/**
 * DEV tool, never packed: draws a sheet of the firmware font's glyphs in one
 * firmware text container, so scripts/build_even_font.py can cut each glyph
 * out of the official simulator's framebuffer. The firmware ships one LVGL
 * font and no plugin may choose another or a size; drawing these glyphs
 * into image containers is the only way to set Even's own letterforms at a
 * smaller size (EvenFont.ts).
 *
 * ?page=N selects a page of the charset; positions are logged as
 * `GLYPHS {...}` from @evenrealities/pretext's own advances.
 */
import {
  waitForEvenAppBridge, TextContainerProperty, CreateStartUpPageContainer,
} from '@evenrealities/even_hub_sdk';
import { getTextWidth } from '@evenrealities/pretext';
import charset from './even-font-charset.json';

const SEPARATOR = '  ';
const LINES_PER_PAGE = 10;
const WIDTH = 560;

const lines: string[][] = [];
let line: string[] = [];
for (const codePoint of charset as number[]) {
  const ch = String.fromCodePoint(codePoint);
  const candidate = [...line, ch].join(SEPARATOR);
  if (line.length > 0 && getTextWidth(candidate) > WIDTH) {
    lines.push(line);
    line = [ch];
  } else {
    line.push(ch);
  }
}
if (line.length > 0) lines.push(line);

const page = Number(new URLSearchParams(location.search).get('page') ?? '0');
const pageLines = lines.slice(page * LINES_PER_PAGE, (page + 1) * LINES_PER_PAGE);
const placed: Array<{ cp: number; line: number; x: number; advance: number }> = [];
pageLines.forEach((glyphs, index) => {
  let prefix = '';
  glyphs.forEach((ch, at) => {
    if (at > 0) prefix += SEPARATOR;
    placed.push({ cp: ch.codePointAt(0)!, line: index, x: getTextWidth(prefix), advance: getTextWidth(ch) });
    prefix += ch;
  });
});
console.info(`GLYPHS ${JSON.stringify({ page, pages: Math.ceil(lines.length / LINES_PER_PAGE), placed })}`);

const bridge = await waitForEvenAppBridge();
await bridge.createStartUpPageContainer(new CreateStartUpPageContainer({
  containerTotalNum: 1,
  textObject: [new TextContainerProperty({
    xPosition: 0, yPosition: 0, width: 576, height: 288, borderWidth: 0, paddingLength: 0,
    containerID: 1, containerName: 'sheet', isEventCapture: 1,
    content: pageLines.map((glyphs) => glyphs.join(SEPARATOR)).join('\n'),
  })],
}));
console.info('SHEET_READY');
