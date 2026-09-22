import { describe, expect, it, vi } from 'vitest';
import * as HudCanvas from './HudCanvasRenderer';
import type { HudVisualFrame } from './HudVisualSurface';
import { SPINE_LABEL_BOX } from './types';
import {
  ASSISTANT_OVERLAY_DISMISS_HINT,
  ASSISTANT_OVERLAY_PAGE_HINT,
  NOTIFICATION_DISMISS_HINT,
  agentTerminalSafetyLabel,
  agentTerminalFocusLabel,
  agentTerminalTranscriptLayout,
  assistantOverlayPages,
  assistantOverlayTextLayout,
  brainstormTranscriptLines,
  brainstormTranscriptRowCount,
  HudCanvasRenderer,
  HUD_VISUAL_GEOMETRY,
  brainstormActionHint,
  brainstormStateLabel,
  hudBrightnessGray,
  hudDisplayTitle,
  parseHudLabeledValue,
  parseHudListItem,
  parseMessagesConversationRow,
  messagesReaderBodyLines,
  messagesFullTextContent,
  MESSAGES_READER_BODY_LINE_CAPACITY,
  messagesConversationTranscript,
  navigationDecisionRows,
  usesNavigationSetupLayout,
  usesMessagesCompactLayout,
  canRenderPlanningMonthGrid,
  fitHudWrappedText,
  planningGridMarker,
  planningGridMatchesMonth,
  parsePlanningMonthTitle,
  planningMonthDays,
  productivityActionHint,
  productivityPhaseLabel,
  productivityPhaseProgress,
  resolveHudOverview,
  showNavigationSecondaryInHeader,
  visibleHudListItems,
  visibleHudLeadingLines,
  visibleHudTailLines,
  wrapHudText,
} from './HudCanvasRenderer';

describe('prompt question rendering', () => {
  function canvasFixture() {
    const text: Array<{ value: string; x: number; y: number; width: number; fontSize: number; font: string }> = [];
    const state: Record<string, unknown> = { font: '12px monospace' };
    const fontSize = () => Number(/([\d.]+)px/.exec(String(state.font))?.[1] ?? 12);
    const width = (value: string) => Array.from(value).length * fontSize() * 0.6;
    Object.assign(state, {
      measureText: (value: string) => ({ width: width(value) }),
      fillText: (value: string, x: number, y: number) => text.push({
        value, x, y, width: width(value), fontSize: fontSize(), font: String(state.font),
      }),
      getImageData: () => ({ data: new Uint8ClampedArray(576 * 288 * 4) }),
    });
    const context = new Proxy(state, { get: (target, key) => target[String(key)] ?? (() => undefined) });
    vi.stubGlobal('document', { createElement: () => ({ width: 0, height: 0, getContext: () => context }) });
    // Keep all production layout/drawing code; only image asset loading and
    // the canvas raster backend are deterministic fixtures here.
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    Object.defineProperty(renderer, 'icons', { value: Promise.resolve({}) });
    return { renderer, text };
  }
  const questionFrame = (question: string): HudVisualFrame => ({
    kind: 'prompt_question', promptId: 'prompt-fixture', question,
    status: '9:41 JOAT listening', micOn: true,
  });

  it('does not contradict a prompted answer with the resting wake-word instruction', async () => {
    const { renderer, text } = canvasFixture();
    try {
      await renderer.render({...questionFrame('What should work do?'),
        status: "9:41 JOAT ONLINE — SAY 'JOAT …'"});
      expect(text.some(({ value }) => /say.*joat/i.test(value))).toBe(false);
      expect(text.some(({ value }) => value === 'speak your answer · double-click: cancel')).toBe(true);
      expect(text.some(({ value, y }) => value === 'JOAT' && y === 20)).toBe(true);
    } finally { vi.unstubAllGlobals(); }
  });

  it('keeps a short spoken question in the prose reading face at its full autofit size', async () => {
    const { renderer, text } = canvasFixture();
    try {
      await renderer.render(questionFrame('Recipient Sam selected. Say the message.'));
      const body = text.filter(({ y }) => y >= 76 && y <= 240);
      expect(body.length).toBeGreaterThan(0);
      expect(body.every(({ font, fontSize }) => font.includes('Helvetica Neue')
        && font.startsWith('400 ') && fontSize === 14)).toBe(true);
    } finally { vi.unstubAllGlobals(); }
  });

  it('draws the entire question including beginning, middle, Unicode and unbroken values at a readable size', async () => {
    const { renderer, text } = canvasFixture();
    const question = `FIRST ${Array.from({ length: 38 }, (_, index) => `clause${index + 1}`).join(' ')} `
      + `漢字 😀 café ${'key'.repeat(28)} FINAL question?`;
    try {
      const tiles = await renderer.render(questionFrame(question));
      const body = text.filter(({ y }) => y >= 80 && y <= 240);
      expect(body.map(({ value }) => value).join('').replace(/\s/g, ''))
        .toBe(question.replace(/\s/g, ''));
      expect(body.every(({ fontSize, x, width }) => fontSize >= 12 && x >= 14 && x + width <= 562)).toBe(true);
      expect(body.every(({ value }) => !value.includes('…'))).toBe(true);
      expect(tiles.map(({ containerId }) => containerId)).toEqual([4, 8, 9, 10]);
      expect(text.some(({ value }) => /confirm action|tap.*confirm/i.test(value))).toBe(false);
    } finally { vi.unstubAllGlobals(); }
  });

  it('fails rendering when the complete question cannot fit instead of keeping only its tail', async () => {
    const { renderer } = canvasFixture();
    try {
      // The wire accepts up to 1000 UTF-16 units; admission is not a promise
      // that every such value fits the optical region at a readable size.
      await expect(renderer.render(questionFrame('W'.repeat(1000)))).rejects.toThrow();
    } finally { vi.unstubAllGlobals(); }
  });
});

describe('hudBrightnessGray', () => {
  it('keeps routine and urgent Mac cards visually distinct', () => {
    expect(hudBrightnessGray(3)).toBeLessThan(hudBrightnessGray(4));
    expect(hudBrightnessGray(3)).toBe(170);
    expect(hudBrightnessGray(4)).toBe(238);
  });

});

describe('bounded HUD copy', () => {
  it('does not reinterpret note punctuation as separately styled metadata', () => {
    expect(parseHudListItem('[2m] Buy milk · oat milk preferred', true)).toEqual({
      age: '2m', primary: 'Buy milk · oat milk preferred', secondary: '',
    });
    expect(parseHudListItem('SHOPPING LIST', true)).toEqual({
      age: '', primary: 'SHOPPING LIST', secondary: '',
    });
  });
  it('keeps the beginning of an incoming notification preview visible', () => {
    expect(visibleHudLeadingLines(['Sender and subject', 'First sentence', 'Older tail'], 2))
      .toEqual(['Sender and subject', 'First sentence…']);
  });
});

describe('Messages full-text reader', () => {
  it('preserves the leading body page as flat text instead of a card', () => {
    expect(messagesFullTextContent([
      'From Dele · unread · 1/2',
      'Opening words remain visible',
      'on this page.',
    ])).toEqual({
      metadata: 'From Dele · unread · 1/2',
      body: 'Opening words remain visible on this page.',
    });
  });

  it('uses the band reclaimed from the title bar for two more body lines', () => {
    expect(MESSAGES_READER_BODY_LINE_CAPACITY).toBe(10);
  });

  it('wraps the reader body by conservative visible columns, not the simulator width estimate', () => {
    expect(messagesReaderBodyLines(
      'Read reply with full reader detail with full reader detail',
    )).toEqual([
      'Read reply with full reader detail with full reader detail',
    ]);
  });
});

describe('QuickNotes exact reader', () => {
  it('keeps older Mac detail lists in their legacy layout', () => {
    expect(HudCanvas.usesQuickNotesReaderLayout('QUICKNOTES · DETAIL', ['Legacy note'])).toBe(false);
    expect(HudCanvas.usesQuickNotesReaderLayout('QUICKNOTES · NOTE', ['Legacy title', 'SUMMARY'])).toBe(false);
  });

  it.each(['QUICKNOTES · DETAIL', 'QUICKNOTES · NOTE'])('draws %s prepared lines without altering their text', (title) => {
    const painted: Array<{text: string; y: number}> = [];
    const context = {
      fillRect() {},
      fillText(text: string, _x: number, y: number) { painted.push({text, y}); },
      measureText(text: string) { return {width: Array.from(text).length * 8.4}; },
    } as unknown as CanvasRenderingContext2D;
    const renderer = Object.create(HudCanvasRenderer.prototype) as {
      fitText(c: CanvasRenderingContext2D, text: string, x: number, y: number, width: number): void;
      drawList(c: CanvasRenderingContext2D, frame: unknown, icons: unknown): void;
    };
    renderer.fitText = (c, text, x, y) => c.fillText(text, x, y);
    const body = ['  leading  spaces\r\n', '\n', '漢😀e\u0301 👩\u200d💻  \r\n', 'x'.repeat(52), 'unbroken-ending\n', 'FINAL  '];
    renderer.drawList(context, {
      kind: 'list', title, items: ['1/1 · Exact text', ...body],
      selectedIndex: 0, selectable: false, hint: 'left x2: back',
    }, {});
    expect(painted.filter(({y}) => y >= 84 && y <= 237)).toEqual(body.map((line, index) => ({
      text: line.replace(/\r?\n$/, ''), y: 84 + index * 17,
    })));
    expect(painted).toContainEqual({text: '1/1 · Exact text', y: 58});
    expect(painted).toContainEqual({text: 'left x2: back', y: 267});
  });
});

describe('productivity escape copy', () => {
  it.each([
    [{ phase: 'verifying', monitoringArmed: false }, 'Back: double-click to Planning'],
    [{ phase: 'saved', monitoringArmed: true }, 'Back: double-click to Planning · monitoring next'],
    [{ phase: 'monitoring', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'completed', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'failed', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'check', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'cancelled', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'not_saved', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'due', monitoringArmed: false }, 'say: complete reminder · Back: double-click to Planning'],
  ] as const)('keeps %o visibly escapable', (model, hint) => {
    expect(productivityActionHint(model)).toBe(hint);
  });
});

describe('flat productivity chrome', () => {
  it('collapses confirmation controls into one static low-bandwidth footer', () => {
    expect(HudCanvas.confirmationActionHint()).toBe(
      'tap: confirm · swipe: cancel · double-click: back · auto-cancel',
    );
  });

  it('uses the same compact left reading axis as the other first-level HUDs', () => {
    expect(HudCanvas.confirmationReviewLayout()).toEqual({
      headerIconX: 36,
      headerTextX: 62,
      headerBaselineY: 60,
      dividerY: 78.5,
      contentX: 36,
      contentWidth: 504,
      promptTopBaselineY: 104,
      promptFontSize: 16,
      promptLineHeight: 20,
      promptMaxLines: 4,
      footerBaselineY: 267,
    });
  });

  it('never draws a full-frame perimeter from the shared status spine', () => {
    let strokes = 0;
    let arcSegments = 0;
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      fillText() {},
      stroke() { strokes += 1; },
      beginPath() {}, moveTo() {}, lineTo() {}, closePath() {},
      arcTo() { arcSegments += 1; },
    } as unknown as CanvasRenderingContext2D;
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      drawIcon(): void;
      drawStatusSpine(
        c: CanvasRenderingContext2D, s: string, m: boolean, i: unknown, showTime: boolean,
      ): void;
    };
    stub.drawIcon = () => {};
    stub.drawStatusSpine(context, '9:41  JOAT ready  bat 87%', true, {} as unknown, true);

    expect(strokes).toBe(0);
    expect(arcSegments).toBe(0);
  });
});

describe('selected visual geometry', () => {
  it('pins the measured reference anchors instead of a generic wide menu', () => {
    expect(HUD_VISUAL_GEOMETRY).toMatchObject({
      statusDividerY: 35.5,
      overviewMainX: 176,
      overviewTrayY: 168,
      listContentX: 93,
    });
  });

  it('uses the reference sentence-case focus heading', () => {
    expect(hudDisplayTitle('MESSAGES · 3')).toBe('Messages · 3');
    expect(hudDisplayTitle('MESSAGES · SETUP')).toBe('Messages · Setup');
    expect(hudDisplayTitle('JOAT')).toBe('JOAT');
    expect(hudDisplayTitle('NOTIFICATIONS')).toBe('Notifications');
    expect(hudDisplayTitle('QUICKNOTES')).toBe('QuickNotes');
    expect(hudDisplayTitle('PLANNING · PLAN · SEPTEMBER 2026')).toBe('Planning · Month · September 2026');
    expect(hudDisplayTitle('PLANNING · PLAN · AUG 31–SEP 6')).toBe('Planning · Week · Aug 31–Sep 6');
    expect(hudDisplayTitle('PLANNING · PLAN · TUE SEP 1')).toBe('Planning · Day · Tue Sep 1');
    expect(hudDisplayTitle('PLANNING · PLAN · SETUP')).toBe('Planning · Setup');
    expect(hudDisplayTitle('Already Mixed')).toBe('Already Mixed');
  });
});

describe('semantic HUD chrome', () => {
  it('labels taps as dismissal and swipes as reading', () => {
    expect(ASSISTANT_OVERLAY_DISMISS_HINT).toBe('TAP TO DISMISS');
    expect(ASSISTANT_OVERLAY_PAGE_HINT).toBe('UP / DOWN TO READ · TAP TO DISMISS');
  });

  it('does not advertise the right-temple exit gesture as notification navigation', () => {
    expect(NOTIFICATION_DISMISS_HINT).toBe('tap: dismiss · left x2: menu');
  });

  it('keeps the recovery instruction in the warning card instead of rendering it twice', () => {
    expect(showNavigationSecondaryInHeader('destination')).toBe(true);
    expect(showNavigationSecondaryInHeader('mode')).toBe(true);
    expect(showNavigationSecondaryInHeader('error')).toBe(false);
  });
});

describe('navigation decisions', () => {
  it('boxes the chosen row at a size readable in motion', () => {
    // A route choice is read while walking, so it uses the legible face and
    // an outline on the SELECTED row — not the dense reading size a
    // stationary list can use, and not a box per row.
    expect(navigationDecisionRows(2)).toEqual([
      { baselineY: 128, boxTopY: 113, boxHeight: 30, fontSize: 15 },
      { baselineY: 162, boxTopY: 147, boxHeight: 30, fontSize: 15 },
    ]);
    expect(navigationDecisionRows(3)).toEqual([
      { baselineY: 128, boxTopY: 113, boxHeight: 30, fontSize: 15 },
      { baselineY: 162, boxTopY: 147, boxHeight: 30, fontSize: 15 },
      { baselineY: 196, boxTopY: 181, boxHeight: 30, fontSize: 15 },
    ]);
    expect(navigationDecisionRows(0)).toEqual([]);
  });

  it('keeps every option and its box clear of the footer', () => {
    const rows = navigationDecisionRows(3);
    const last = rows[rows.length - 1]!;
    expect(last.boxTopY + last.boxHeight).toBeLessThan(267);
  });
});

describe('flat navigation entry', () => {
  it('reserves the compact instruction treatment for the non-selectable Navigation entry', () => {
    expect(usesNavigationSetupLayout('NAVIGATION', false)).toBe(true);
    expect(usesNavigationSetupLayout('NAVIGATION', true)).toBe(false);
    expect(usesNavigationSetupLayout('MESSAGES', false)).toBe(false);
  });
});

describe('compact focus list rows', () => {
  it('gives a Planning day the same ten dense rows as every other list', () => {
    // The two-line agenda variant existed only because a day row spelled out
    // "16:30 · EVENT · Title". Rows now read "[16:30] \u25a1 Title", so they
    // fit one line — and with the page name moved into the status spine,
    // rows start at y60 instead of y84: ten entries per glance.
    expect(HudCanvas.planningFocusRows('PLANNING · PLAN · TUE SEP 1', 4)).toEqual([
      { primaryBaselineY: 60, railTopY: 50, railHeight: 19 },
      { primaryBaselineY: 80, railTopY: 70, railHeight: 19 },
      { primaryBaselineY: 100, railTopY: 90, railHeight: 19 },
      { primaryBaselineY: 120, railTopY: 110, railHeight: 19 },
    ]);
    expect(HudCanvas.planningFocusRows('PLANNING · PLAN · TUE SEP 1', 12)).toHaveLength(10);
    // No row carries a second line any more.
    expect(
      HudCanvas.planningFocusRows('PLANNING · PLAN · TUE SEP 1', 4)[0]?.secondaryBaselineY,
    ).toBeUndefined();
  });

  it('uses the full vertical budget: ten one-line rows per glance', () => {
    // Pitch divides rows; separator lines, dead bands, and the in-body
    // title are gone. The wearer's ruling: unused canvas on a content page
    // is waste, not calm — and the page's name belongs in the spine.
    expect(HudCanvas.planningFocusRows('PLANNING · PLAN · AUG 31–SEP 6', 7)).toEqual([
      { primaryBaselineY: 60, railTopY: 50, railHeight: 19 },
      { primaryBaselineY: 80, railTopY: 70, railHeight: 19 },
      { primaryBaselineY: 100, railTopY: 90, railHeight: 19 },
      { primaryBaselineY: 120, railTopY: 110, railHeight: 19 },
      { primaryBaselineY: 140, railTopY: 130, railHeight: 19 },
      { primaryBaselineY: 160, railTopY: 150, railHeight: 19 },
      { primaryBaselineY: 180, railTopY: 170, railHeight: 19 },
    ]);
  });

  it('names the page in the status spine instead of a second title bar', () => {
    expect(HudCanvas.hudSpinePageTitle({
      kind: 'list', title: 'MESSAGES · CONVERSATION',
    } as never)).toBe('Messages · Conversation');
    expect(HudCanvas.hudSpinePageTitle({ kind: 'navigation' } as never)).toBe('Navigation');
    // Home is JOAT: the spine keeps the assistant's own name there.
    expect(HudCanvas.hudSpinePageTitle({ kind: 'overview' } as never)).toBe('');
  });

  it('recognizes only the small semantic marker vocabulary', () => {
    // Markers indicate a row's KIND (● recording, □ todo, – key point) from
    // the design-guidelines firmware-safe set; anything else is content.
    expect(HudCanvas.focusListMarker('● Client standup')).toEqual({
      glyph: '●', emphasis: 'content',
    });
    expect(HudCanvas.focusListMarker('□ Email release notes')).toEqual({
      glyph: '□', emphasis: 'annotation',
    });
    expect(HudCanvas.focusListMarker('– Ship Friday')).toEqual({
      glyph: '–', emphasis: 'annotation',
    });
    expect(HudCanvas.focusListMarker('Plain row')).toBeUndefined();
    expect(HudCanvas.focusListMarker('★ Unknown glyph')).toBeUndefined();
    expect(HudCanvas.focusListMarker('●NoSpace')).toBeUndefined();
  });

  it('reserves one shared title edge only for lists that mix marked rows', () => {
    // The QuickNotes list mixes recordings and text notes; unmarked rows
    // must indent so every title starts at the same x (no ragged edge).
    expect(HudCanvas.listReservesMarkerColumn([
      '[4m] ● Client standup',
      '[2h] Ask Adaeze about the design review',
    ])).toBe(true);
    // TASKS/MESSAGES never carry markers — they lose no width.
    expect(HudCanvas.listReservesMarkerColumn([
      '[2m] Adele · Can you send the latest mockups?',
      'Buy filters · Due today 18:00',
    ])).toBe(false);
    expect(HudCanvas.listReservesMarkerColumn([])).toBe(false);
  });

  it('applies the one dense list language to every title', () => {
    expect(HudCanvas.planningFocusRows('MESSAGES', 4)).toHaveLength(4);
    // An eleven-row note now fits ten rows on the first glance; the rest is
    // one swipe away, not three.
    expect(HudCanvas.planningFocusRows('QUICKNOTES · NOTE', 11)).toHaveLength(10);
    expect(HudCanvas.planningFocusRows('QUICKNOTES', 4)).toHaveLength(4);
    expect(HudCanvas.planningFocusRows('QUICKNOTES · RECORDING', 3)).toHaveLength(3);
    expect(
      HudCanvas.planningFocusRows('MESSAGES', 4)[0]?.secondaryBaselineY,
    ).toBeUndefined();
  });
});

describe('compact productivity outcome geometry', () => {
  it('uses a compact text stack without a card or progress strip', () => {
    expect(HudCanvas.productivityOutcomeLayout()).toEqual({
      titleBaselineY: 98,
      dividerY: 136.5,
      primaryBaselineY: 158,
      secondaryBaselineY: 181,
      footerBaselineY: 267,
    });
  });
});

describe('assistant overlay text fitting', () => {
  function assistantCanvasFixture() {
    const text: Array<{ value: string; x: number; y: number; width: number }> = [];
    const state: Record<string, unknown> = { font: '14px Helvetica Neue' };
    const fontSize = () => Number(/([\d.]+)px/.exec(String(state.font))?.[1] ?? 14);
    const width = (value: string) => Array.from(value).length * fontSize() * 0.6;
    Object.assign(state, {
      measureText: (value: string) => ({ width: width(value) }),
      fillText: (value: string, x: number, y: number) => text.push({ value, x, y, width: width(value) }),
      getImageData: () => ({ data: new Uint8ClampedArray(576 * 288 * 4) }),
    });
    const context = new Proxy(state, { get: (target, key) => target[String(key)] ?? (() => undefined) });
    vi.stubGlobal('document', { createElement: () => ({ width: 0, height: 0, getContext: () => context }) });
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    Object.defineProperty(renderer, 'icons', { value: Promise.resolve({}) });
    return { renderer, text };
  }

  const overviewWithAnswer = (text: string, revision = 1): HudVisualFrame => ({
    kind: 'overview', status: '9:41 JOAT answering', micOn: true,
    main: '07\n32', mainBrightness: 4, context: '', tray: '', hint: '',
    assistantOverlay: { text, presentation: 'answer', revision },
  });

  it('keeps a four-line assistant reply below the header divider and above its footer', () => {
    const layout = assistantOverlayTextLayout(4, 17);

    expect(layout).toMatchObject({
      dividerY: 92,
      firstGlyphTopY: 106,
      lastGlyphBottomY: 186,
      footerBaselineY: 214,
    });
    expect(layout.firstGlyphTopY).toBeGreaterThan(layout.dividerY);
    expect(layout.lastGlyphBottomY).toBeLessThan(layout.footerBaselineY);
  });

  it('keeps only the newest four lines when the minimum font still overflows', () => {
    const fitted = fitHudWrappedText(
      'one two three four five six seven eight nine ten eleven twelve thirteen fourteen',
      72,
      4,
      (value, fontSize) => value.length * fontSize * 0.5,
    );

    expect(fitted.fontSize).toBe(12);
    expect(fitted.lines).toHaveLength(4);
    expect(fitted.lines[0]).toMatch(/^…/);
    expect(fitted.lines.join(' ')).toContain('fourteen');
  });

  it('accounts for every word of the recorded four-sentence answer across readable pages', () => {
    const answer = 'Sunlight contains many colors, and Earth’s atmosphere scatters shorter blue '
      + 'wavelengths more strongly than longer red wavelengths, making the daytime sky look blue. '
      + 'At sunset, sunlight travels through more atmosphere, so most blues are scattered away '
      + 'before the light reaches you. The remaining direct light is richer in reds and oranges, '
      + 'which paints the Sun and nearby sky in warm colors. Dust, smoke, clouds, and pollution '
      + 'can intensify or mute those sunset colors.';
    // 7.2px matches the production Helvetica face closely enough to retain
    // the nine-line simulator case that once produced a one-word page 3.
    const pages = assistantOverlayPages(answer, 386, 4, (value) => Array.from(value).length * 7.2);

    expect(pages.length).toBeGreaterThan(1);
    expect(pages[0]?.join(' ')).toMatch(/^Sunlight contains many colors/);
    expect(pages[0]?.join(' ')).toMatch(/sky look blue\.$/);
    expect(pages[1]?.join(' ')).toMatch(/^At sunset/);
    expect(pages[pages.length - 1]?.join(' ')).toMatch(/sunset colors\.$/);
    expect(pages.flat().join(' ').replace(/\s+/g, ' ').trim()).toBe(answer);
    expect(pages.flat().some((line) => line.includes('…'))).toBe(false);
    expect(Math.max(...pages.map((page) => page.length))
      - Math.min(...pages.map((page) => page.length))).toBeLessThanOrEqual(2);
    expect(pages[pages.length - 1]?.length).toBeGreaterThan(1);
  });

  it('renders Paris as an answer above the old pill region and exposes the final long-answer page', async () => {
    const { renderer, text } = assistantCanvasFixture();
    const answer = 'FIRST opening words explain the blue daytime sky with enough detail to wrap. '
      + 'Second sentence explains that sunset light travels through more atmosphere. '
      + 'Third sentence explains that shorter wavelengths scatter away from the viewer. '
      + 'FINAL words say the remaining reds and oranges become visible.';
    try {
      await renderer.render(overviewWithAnswer('Paris.'));
      expect(text.find(({ value }) => value === 'Paris.')?.y).toBeLessThan(196);

      text.length = 0;
      const frame = overviewWithAnswer(answer);
      await renderer.render(frame);
      expect(text.some(({ value }) => value.startsWith('FIRST opening words'))).toBe(true);
      for (let page = 0; page < 10 && renderer.scrollAssistantOverlay('down'); page += 1) {
        // Move to the final bounded page before the next production render.
      }
      text.length = 0;
      await renderer.render(frame);
      expect(text.some(({ value }) => value.includes('FINAL words'))).toBe(true);
      expect(text.some(({ value }) => /\d+\/\d+/.test(value))).toBe(true);
    } finally { vi.unstubAllGlobals(); }
  });

  it('opens a repeated answer on page one instead of retaining the prior reading position', async () => {
    const { renderer, text } = assistantCanvasFixture();
    const answer = 'FIRST sentence is deliberately long enough to make this answer span several pages. '
      + 'Second sentence carries more explanation for the reader. '
      + 'FINAL sentence confirms that the repeated answer is complete.';
    try {
      await renderer.render(overviewWithAnswer(answer, 1));
      expect(renderer.scrollAssistantOverlay('down')).toBe(true);
      text.length = 0;
      await renderer.render(overviewWithAnswer(answer, 1));
      expect(text.some(({ value }) => value.startsWith('FIRST sentence'))).toBe(false);

      text.length = 0;
      await renderer.render(overviewWithAnswer(answer, 2));
      expect(text.some(({ value }) => value.startsWith('FIRST sentence'))).toBe(true);
      expect(text.some(({ value }) => value.startsWith('1/'))).toBe(true);
    } finally { vi.unstubAllGlobals(); }
  });
});

describe('Planning month geometry', () => {
  it('recognizes the native Planning month title and creates a stable Monday-first grid', () => {
    const month = parsePlanningMonthTitle('PLANNING · PLAN · SEPTEMBER 2026');

    expect(month).toEqual({ year: 2026, month: 8, label: 'SEPTEMBER' });
    expect(planningMonthDays(month!).slice(0, 7)).toEqual([
      { day: 31, inMonth: false },
      { day: 1, inMonth: true },
      { day: 2, inMonth: true },
      { day: 3, inMonth: true },
      { day: 4, inMonth: true },
      { day: 5, inMonth: true },
      { day: 6, inMonth: true },
    ]);
    expect(planningMonthDays(month!)).toHaveLength(42);
    const days = planningMonthDays(month!);
    expect(days[days.length - 1]).toEqual({ day: 11, inMonth: false });

    const planningGrid = Array.from({ length: 42 }, (_, index) => {
      const value = new Date(Date.UTC(2026, 7, 31 + index));
      return {
        day: value.toISOString().slice(0, 10),
        eventCount: index === 1 ? 1 : 0,
        reminderCount: index === 1 ? 1 : 0,
        inMonth: value.getUTCMonth() === 8,
      };
    });
    expect(planningGridMatchesMonth(month!, planningGrid)).toBe(true);
    expect(planningGridMarker(planningGrid[1]!)).toBe('E1 R1');
    expect(planningGridMarker(planningGrid[0]!)).toBe('');
    expect(canRenderPlanningMonthGrid(month, planningGrid, true, 0, 11)).toBe(true);
    expect(canRenderPlanningMonthGrid(month, planningGrid, true, 5, 11)).toBe(true);
    expect(canRenderPlanningMonthGrid(month, planningGrid, true, 6, 11)).toBe(false);
    expect(planningGridMatchesMonth(month!, [
      ...planningGrid.slice(0, 1),
      { ...planningGrid[1]!, day: '2026-09-02' },
      ...planningGrid.slice(2),
    ])).toBe(false);
  });

  it('does not mistake a week, day, or arbitrary list for a calendar month', () => {
    expect(parsePlanningMonthTitle('PLANNING · PLAN · SEP 1–7')).toBeUndefined();
    expect(parsePlanningMonthTitle('PLANNING · PLAN · TUE SEP 1')).toBeUndefined();
    expect(parsePlanningMonthTitle('SEPTEMBER 2026')).toBeUndefined();
    expect(parsePlanningMonthTitle('PLANNING · PLAN · JANUARY 0099')).toBeUndefined();
  });

  it('marks the selected month week with a slim rail instead of an enclosing box', () => {
    expect(HudCanvas.planningMonthSelectionRail(3)).toEqual({
      x: 28,
      y: 153,
      width: 3,
      height: 22,
    });
  });
});

describe('flat confirmation actions', () => {
  it('does not expose countdown-dependent visual geometry', () => {
    expect(HudCanvas.confirmationReviewLayout()).not.toHaveProperty('progressY');
    expect(HudCanvas.confirmationReviewLayout()).not.toHaveProperty('timerBaselineY');
  });
});

describe('productivity HUD state labels', () => {
  it('keeps native verification, uncertainty, and due alarms visually distinct', () => {
    expect(productivityPhaseLabel('verifying')).toBe('VERIFYING');
    expect(productivityPhaseLabel('saved')).toBe('SAVED');
    expect(productivityPhaseLabel('monitoring')).toBe('MONITORING');
    expect(productivityPhaseLabel('completed')).toBe('COMPLETED');
    expect(productivityPhaseLabel('failed')).toBe('FAILED');
    expect(productivityPhaseLabel('check')).toBe('UNCERTAIN');
    expect(productivityPhaseLabel('due')).toBe('DUE NOW');
    expect(productivityPhaseProgress('saved')).toBe(1);
    expect(productivityPhaseProgress('monitoring')).toBe(1);
    expect(productivityPhaseProgress('completed')).toBe(1);
    expect(productivityPhaseProgress('due')).toBe(1);
    expect(productivityPhaseProgress('verifying')).toBe(0.45);
    expect(productivityPhaseProgress('check')).toBe(0.18);
  });
});

describe('Brainstorm wearable actions', () => {
  it('keeps a dense terminal tail on complete speaker lines', () => {
    expect(brainstormTranscriptLines(
      'CODEX > first idea\nCLAUDE > constraint\nHERMES > practical next step',
      42,
      2,
      (value) => value.length,
    )).toEqual(['…CLAUDE > constraint', 'HERMES > practical next step']);
  });

  it('labels terminal recovery states without implying an unrelated tool failure', () => {
    expect(agentTerminalSafetyLabel({ state: 'approval_required', noToolsChecked: false }))
      .toBe('APPROVAL REQUIRED');
    expect(agentTerminalSafetyLabel({ state: 'unavailable', noToolsChecked: false }))
      .toBe('CONNECTION UNAVAILABLE');
    expect(agentTerminalSafetyLabel({ state: 'blocked', noToolsChecked: false }))
      .toBe('TERMINAL BLOCKED');
    for (const state of ['ready', 'running'] as const) {
      for (const noToolsChecked of [false, true]) {
        expect(agentTerminalSafetyLabel({ state, noToolsChecked })).toBe('');
      }
    }
  });

  it('makes a running Agent Terminal a transcript-first surface', () => {
    expect(agentTerminalTranscriptLayout({
      state: 'running', noToolsChecked: true, topic: 'hermes · running',
    }))
      .toEqual({
        // A2A work is not a streaming connection; match the chooser state.
        summary: 'HERMES > WORKING',
        // Dense: header + LANES in two tight rows, 12 transcript rows.
        dividerY: 78.5,
        transcriptBaselineY: 92,
        transcriptLineCapacity: 12,
        showRecoveryDetails: false,
      });
  });

  it('keeps connection and policy detail on a recoverable terminal state', () => {
    expect(agentTerminalTranscriptLayout({
      state: 'approval_required', noToolsChecked: false, topic: 'hermes · waiting',
    }))
      .toEqual({
        summary: 'HERMES > NEEDS APPROVAL',
        dividerY: 118.5,
        transcriptBaselineY: 132,
        transcriptLineCapacity: 9,
        showRecoveryDetails: true,
      });
  });

  it('names the lane the wearer is actually focused on', () => {
    // The header was hardcoded HERMES, so a wearer on a calendar lane or a
    // gateway session read someone else's name above their own output.
    expect(agentTerminalTranscriptLayout({
      state: 'running', noToolsChecked: false, topic: 'calendar · running · 2 waiting',
    }).summary).toBe('CALENDAR > WORKING');
    expect(agentTerminalFocusLabel('agent:hermes:nightly · running'))
      .toBe('AGENT:HERMES:NIGHTLY');
    // A gateway key we do not choose must not run off the spine.
    expect(agentTerminalFocusLabel(`${'x'.repeat(90)} · running`).length)
      .toBeLessThanOrEqual(24);
    // No lane named yet: the house default, not an empty prefix.
    expect(agentTerminalFocusLabel(undefined)).toBe('AGENT TERMINAL');
    expect(agentTerminalFocusLabel('')).toBe('AGENT TERMINAL');
  });

  it('discloses scroll, terminal handoff, the verb layer, and cancel', () => {
    const hint = brainstormActionHint({
      screen: 'brainstorm',
      state: 'running',
      noToolsChecked: true,
    });
    expect(hint).toContain('swipe: scroll');
    expect(hint).toContain('tap: terminal');
    expect(hint).toContain('left x2: cancel');
    // An open-ended capability nobody knows about is not a capability.
    // JOAT can act on any reply — "make that shorter", "send that to
    // hermes" — and nothing on the glass said so.
    expect(hint).toContain('send that to hermes');
  });

  it('keeps the diagnostic test gesture off an ordinary ready Brainstorm', () => {
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'ready',
      noToolsChecked: true,
    })).toBe('tap: speak · double-click: menu');
  });

  it('shows the diagnostic test gesture only when the Mac explicitly arms it', () => {
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'ready',
      noToolsChecked: true,
      acceptanceArmed: true,
    } as Parameters<typeof brainstormActionHint>[0])).toBe('tap: speak · right/ring x2: test · left x2: menu');
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'ready',
      noToolsChecked: false,
    })).not.toContain('test');
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'acceptance_ready',
      noToolsChecked: true,
    })).toBe('tap: start test · left x2: menu');
    expect(brainstormStateLabel('acceptance_ready')).toBe('TEST READY');
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'topic_capture',
      noToolsChecked: true,
    })).toBe('speak your question · left x2: cancel');
    expect(brainstormStateLabel('topic_capture')).toBe('SPEAK NOW');
  });
});

describe('parseHudLabeledValue', () => {
  it('treats a normal assistant reply as content, never as a label', () => {
    expect(parseHudLabeledValue('Your meeting starts in 19 minutes.', 'REPLY')).toEqual({
      label: 'REPLY',
      value: 'Your meeting starts in 19 minutes.',
      structured: false,
    });
  });

  it('preserves the approved label/value fixture shape', () => {
    expect(parseHudLabeledValue('NEXT\nDesign review · 4:30', 'REPLY')).toEqual({
      label: 'NEXT',
      value: 'Design review · 4:30',
      structured: true,
    });
  });
});

describe('resolveHudOverview', () => {
  it('turns a status-only connecting boot into one intentional focus state', () => {
    expect(resolveHudOverview({
      status: '4:28   JOAT connecting',
      main: '  ',
      context: '',
      tray: '\n',
    })).toEqual({
      kind: 'empty',
      title: 'Connecting',
      primary: 'Waiting for JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: 'left x2: menu · long-press: mic',
    });
  });

  it('never mixes stale Overview rows into a reconnecting frame', () => {
    expect(resolveHudOverview({
      status: '4:28   JOAT reconnecting',
      main: 'REPLY\nOld answer from the prior connection',
      context: 'LAST QUERY\nold query',
      tray: '2 notifications',
    })).toEqual({
      kind: 'empty',
      title: 'Reconnecting',
      primary: 'Restoring JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: 'left x2: menu · long-press: mic',
    });
  });

  it('never invents tray or context chrome for a partial main-only frame', () => {
    expect(resolveHudOverview({
      status: '9:41   JOAT mic off',
      main: 'Your meeting starts in 19 minutes.',
      context: '',
      tray: '',
    })).toEqual({
      kind: 'content',
      main: {
        label: 'REPLY',
        value: 'Your meeting starts in 19 minutes.',
        structured: false,
      },
    });
  });

  it('labels a typed incoming Messages card as MESSAGE, never REPLY', () => {
    expect(resolveHudOverview({
      status: '9:41   JOAT listening',
      main: '(201) 462-3963 · Messages\nCajun Corner order confirmed',
      mainKind: 'notification',
      context: '',
      tray: '',
    })).toEqual({
      kind: 'content',
      main: {
        label: 'MESSAGE',
        value: '(201) 462-3963 · Messages Cajun Corner order confirmed',
        structured: false,
      },
    });
  });

  it('preserves every semantic section in the populated Overview fixture', () => {
    expect(resolveHudOverview({
      status: '9:41   JOAT ready',
      main: 'NEXT\nDesign review · 4:30',
      context: 'LAST QUERY\nnearest coffee shop?',
      tray: '2 notifications waiting',
    })).toEqual({
      kind: 'content',
      main: {
        label: 'NEXT',
        value: 'Design review · 4:30',
        structured: true,
      },
      tray: '2 notifications',
      context: {
        label: 'LAST QUERY',
        value: 'nearest coffee shop?',
        structured: true,
      },
    });
  });
});

describe('parseHudListItem', () => {
  it('extracts only a real Mac-provided age from a message row', () => {
    expect(parseHudListItem('[18m] Marcus · Heading to the lab in 10.')).toEqual({
      primary: 'Marcus',
      secondary: 'Heading to the lab in 10.',
      age: '18m',
    });
  });

  it('does not invent an age for loading, empty, or settings rows', () => {
    expect(parseHudListItem('Loading recent notifications…')).toEqual({
      primary: 'Loading recent notifications…',
      secondary: '',
      age: '',
    });
  });
});

describe('parseMessagesConversationRow', () => {
  it('keeps bespoke layouts only for the transcript and the reader', () => {
    // Every other Messages page — thread picker, compose, access, retry —
    // speaks the one dense focus-list language like the rest of the HUD.
    expect(usesMessagesCompactLayout('MESSAGES · CONVERSATION')).toBe(true);
    expect(usesMessagesCompactLayout('MESSAGES · MESSAGE')).toBe(true);
    expect(usesMessagesCompactLayout('MESSAGES')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · COMPOSE')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · ACCESS')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · SETUP')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · RETRY')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · EMPTY')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · READ ONLY')).toBe(false);
    expect(usesMessagesCompactLayout('NOTIFICATIONS')).toBe(false);
  });

  it('separates incoming, outgoing, and voice-action rows', () => {
    expect(parseMessagesConversationRow('Dele · Are you coming?')).toEqual({
      kind: 'incoming', label: 'Dele', body: 'Are you coming?',
    });
    expect(parseMessagesConversationRow('You · On my way')).toEqual({
      kind: 'outgoing', label: 'You', body: 'On my way',
    });
    expect(parseMessagesConversationRow('Say “JOAT, reply saying …”')).toEqual({
      kind: 'instruction', label: '', body: 'Say “JOAT, reply saying …”',
    });
  });

  it('keeps a compact seven-line transcript free of header and action-card rows', () => {
    const transcript = messagesConversationTranscript([
      'Latest messages — down for older',
      'Dele · newest incoming',
      'You · newest outgoing',
      'Dele · third message',
      'Dele · fourth message',
      'You · fifth message',
      'Dele · sixth message',
      'Dele · seventh message',
      'Say “JOAT, reply saying …”',
      'Mic off? Long press to enable.',
    ], 0);

    expect(transcript.heading).toBe('Latest messages — down for older');
    expect(transcript.rows).toEqual([
      { kind: 'incoming', label: 'Dele', body: 'newest incoming' },
      { kind: 'outgoing', label: 'You', body: 'newest outgoing' },
      { kind: 'incoming', label: 'Dele', body: 'third message' },
      { kind: 'incoming', label: 'Dele', body: 'fourth message' },
      { kind: 'outgoing', label: 'You', body: 'fifth message' },
      { kind: 'incoming', label: 'Dele', body: 'sixth message' },
      { kind: 'incoming', label: 'Dele', body: 'seventh message' },
    ]);
  });

  it('keeps an older selected speaker reachable in the compact transcript window', () => {
    const transcript = messagesConversationTranscript([
      'Latest messages — down for older',
      'Dele · one',
      'You · two',
      'Dele · three',
      'Dele · four',
      'You · five',
      'Dele · six',
      'Dele · seven',
      'Dele · eight',
      'Say “JOAT, reply saying …”',
      'Mic off? Long press to enable.',
    ], 8);

    expect(transcript.rows.map(({ body }) => body)).toEqual([
      'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
    ]);
  });
});

describe('visibleHudListItems', () => {
  it('keeps later Mac rows reachable on the three-row optical page', () => {
    const items = ['one', 'two', 'three', 'four', 'five'];
    expect(visibleHudListItems(items, 0)).toEqual([
      { item: 'one', index: 0 },
      { item: 'two', index: 1 },
      { item: 'three', index: 2 },
    ]);
    expect(visibleHudListItems(items, 4)).toEqual([
      { item: 'three', index: 2 },
      { item: 'four', index: 3 },
      { item: 'five', index: 4 },
    ]);
  });
});

describe('HUD text visibility', () => {
  it('keeps the newest streamed reply lines visible after overflow', () => {
    expect(visibleHudTailLines(['already', 'read', 'newest', 'tail'], 2))
      .toEqual(['…newest', 'tail']);
  });

  it('wraps the complete maximum-length confirmation without an ellipsis', () => {
    const prompt = 'Send the confidential design review summary to Adele and Marcus after the meeting including every requested attachment?';
    expect(prompt.length).toBeLessThanOrEqual(120);
    const lines = wrapHudText(prompt, 44, (value) => value.length);
    expect(lines.length).toBeLessThanOrEqual(3);
    expect(lines.join(' ')).toBe(prompt);
    expect(lines.join('')).not.toContain('…');
  });
});

describe('action pill outcomes', () => {
  it('renders transient receipts as a strip and truth states as pages', () => {
    // Wearer's ruling (2026-09-01): a save/delete receipt is a small wide
    // pill like the platform's own listening indicator, not a page takeover.
    for (const phase of ['verifying', 'saved', 'monitoring', 'completed',
      'cancelled', 'not_saved'] as const) {
      expect(HudCanvas.usesActionPillLayout(phase)).toBe(true);
    }
    // failed/check decide whether a retry is safe; due is an interactive
    // alert with an acknowledged delivery. Those must be read, not glanced.
    for (const phase of ['failed', 'check', 'due'] as const) {
      expect(HudCanvas.usesActionPillLayout(phase)).toBe(false);
    }
  });

  it('keeps the strip inside the content field, clear of spine and footer', () => {
    expect(HudCanvas.ACTION_PILL.y).toBeGreaterThan(36);
    expect(HudCanvas.ACTION_PILL.y + HudCanvas.ACTION_PILL.height).toBeLessThan(260);
    expect(HudCanvas.ACTION_PILL.x + HudCanvas.ACTION_PILL.width).toBeLessThanOrEqual(576);
  });
});

describe('home dashboard content', () => {
  it('shows the notifications themselves, never a count', () => {
    // The reference HUDs put the cards on the resting screen; a counter
    // would only tell the wearer to open a page they should not need.
    expect(HudCanvas.parseHudNotificationRows(
      'Calendar · 8m · Design review moved to 4:30\nMail · 23m · Q3 plan is ready',
    )).toEqual([
      { source: 'Calendar', age: '8m', headline: 'Design review moved to 4:30' },
      { source: 'Mail', age: '23m', headline: 'Q3 plan is ready' },
    ]);
  });

  it('keeps a headline that contains the separator intact', () => {
    expect(HudCanvas.parseHudNotificationRows('Mail · 2m · Q3 · final draft')[0])
      .toEqual({ source: 'Mail', age: '2m', headline: 'Q3 · final draft' });
  });

  it('treats an unstructured line as a plain headline', () => {
    expect(HudCanvas.parseHudNotificationRows('Reminder monitoring armed'))
      .toEqual([{ source: '', age: '', headline: 'Reminder monitoring armed' }]);
    expect(HudCanvas.parseHudNotificationRows('')).toEqual([]);
  });

  it('reads the clock and date from the phone, not the Mac', () => {
    expect(HudCanvas.hudClockParts('9:41')).toEqual({ hours: '09', minutes: '41' });
    expect(HudCanvas.hudClockParts('')).toEqual({ hours: '--', minutes: '--' });
    expect(HudCanvas.hudDateLabel(new Date(2026, 8, 1))).toBe('Tue 9/1');
  });
});

describe('navigation map projection', () => {
  const panel = {
    latitude: 41.9147, longitude: -87.6379, zoom: 16,
    width: 248, height: 168, image: {} as CanvasImageSource,
  };

  it('puts the panel centre exactly at the pane centre', () => {
    const bounds = HudCanvas.NAVIGATION_MAP_PANEL;
    const [x, y] = HudCanvas.projectMercatorPanel(
      { latitude: panel.latitude, longitude: panel.longitude }, panel, bounds,
    );
    expect(x).toBeCloseTo(bounds.x + bounds.width / 2, 6);
    expect(y).toBeCloseTo(bounds.y + bounds.height / 2, 6);
  });

  it('places north up and east right, at the Mapbox 512px tile scale', () => {
    const bounds = HudCanvas.NAVIGATION_MAP_PANEL;
    const north = HudCanvas.projectMercatorPanel(
      { latitude: panel.latitude + 0.001, longitude: panel.longitude }, panel, bounds,
    );
    const east = HudCanvas.projectMercatorPanel(
      { latitude: panel.latitude, longitude: panel.longitude + 0.001 }, panel, bounds,
    );
    expect(north[1]).toBeLessThan(bounds.y + bounds.height / 2);
    expect(east[0]).toBeGreaterThan(bounds.x + bounds.width / 2);
    // 512·2^16 px span 360°, so 0.001° of longitude is 93.2px — which is
    // also what makes the 248px pane ≈220m wide. If this drifts, JOAT's
    // route stops sitting on the roads the Mac drew.
    expect(east[0] - (bounds.x + bounds.width / 2)).toBeCloseTo(93.2, 0);
  });

  it('keeps changing mapped-guidance facts inside the left tile column', () => {
    const widths: Array<number | undefined> = [];
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      drawActiveNavigation(c: CanvasRenderingContext2D, navigation: unknown): void;
      drawManeuver(): void;
      fitText(): void;
      drawWrappedText(): void;
      drawNavigationMap(): void;
      arrivalClock(): string;
      drawNavigationProgress(c: CanvasRenderingContext2D, progress: number, width?: number): void;
      drawNavigationFooter(c: CanvasRenderingContext2D, copy: string, width?: number): void;
    };
    stub.drawManeuver = () => {};
    stub.fitText = () => {};
    stub.drawWrappedText = () => {};
    stub.drawNavigationMap = () => {};
    stub.arrivalClock = () => '';
    stub.drawNavigationProgress = (_context, _progress, width) => { widths.push(width); };
    stub.drawNavigationFooter = (_context, _copy, width) => { widths.push(width); };
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      fillText() {},
    } as unknown as CanvasRenderingContext2D;

    stub.drawActiveNavigation(context, {
      view: 'guidance', primary: 'Turn right', secondary: 'onto Main Street',
      hint: 'Then continue', interaction: 'none', options: [], selectedIndex: 0,
      mode: 'walking', maneuver: 'turn_right', maneuverDistanceM: 40,
      remainingDistanceM: 900, etaMinutes: 12, progress: 0.25,
      map: {
        geometry: [
          { latitude: panel.latitude, longitude: panel.longitude },
          { latitude: panel.latitude + 0.001, longitude: panel.longitude + 0.001 },
        ],
        position: { latitude: panel.latitude, longitude: panel.longitude },
        panel,
      },
    });

    // The left guidance pane ends at x=272, before the x=288 image-tile
    // seam. A full-width progress/footer dirties the bottom-right map tile
    // for every distance update and adds one unnecessary serial BLE write.
    expect(widths).toEqual([236, 236]);
  });

  it('draws the mapped-guidance live band before the image-tile seam', () => {
    const bounds: Array<{ x: number; width: number }> = [];
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      fitText(
        c: CanvasRenderingContext2D, text: string, x: number, y: number, width: number,
      ): void;
      drawNavigationProgress(c: CanvasRenderingContext2D, progress: number, width?: number): void;
      drawNavigationFooter(c: CanvasRenderingContext2D, copy: string, width?: number): void;
    };
    stub.fitText = (_context, _text, x, _y, width) => { bounds.push({ x, width }); };
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      fillRect(x: number, _y: number, width: number) { bounds.push({ x, width }); },
    } as unknown as CanvasRenderingContext2D;

    stub.drawNavigationProgress(context, 0.4, 236);
    stub.drawNavigationFooter(context, '900 m left · 12 min', 236);

    expect(bounds).toHaveLength(3);
    expect(bounds.every(({ x, width }) => x + width < 288)).toBe(true);
  });
});

describe('assistant answer and receipt semantics', () => {
  it('uses an explicit receipt type instead of guessing from string length', () => {
    for (const text of [
      'Reply sent.',
      'Reply cancelled.',
      'New message cancelled.',
      'Saved QuickNote: Ship it today.',
    ]) {
      expect(HudCanvas.usesAssistantPillLayout(text, 'receipt')).toBe(true);
    }
    expect(HudCanvas.usesAssistantPillLayout('Paris.', 'answer')).toBe(false);
    expect(HudCanvas.usesAssistantPillLayout(
      'I checked the calendar and reminders. Your design review is next, '
      + 'and reminder monitoring is armed for this evening.',
      'answer',
    )).toBe(false);
    expect(HudCanvas.usesAssistantPillLayout('line one\nline two', 'receipt')).toBe(false);
    expect(HudCanvas.usesAssistantPillLayout('   ', 'receipt')).toBe(false);
  });
});

describe('every speaker row splits dim tag from bright words', () => {
  // Verified against the real HudSink output, not invented: these are the
  // exact rows it paints, including the repair label the engine builds as
  // "<speaker> — repair <n>".
  const TAG = /^([A-Z][A-Z0-9 .—-]{0,22}) > /;

  it('tags every label the engine actually produces', () => {
    for (const row of [
      'CODEX > cache on the session id',
      'CLAUDE > that breaks on handoff',
      'VERIFIER > [1] OK — quote found',
      'YOU > I think that is the wrong layer',
      'JOAT > AGREED: key on the generation',
      // A repair rendered FLAT before this: no dim tag, no bright body, on
      // the one row where the split matters most.
      'CODEX — REPAIR 1 > corrected with a real quote',
      'CLAUDE — ROUND 2 > building on that',
    ]) {
      expect(TAG.test(row), row).toBe(true);
    }
  });

  it('never claims a tag in ordinary prose', () => {
    for (const row of [
      'the cache is the wrong layer',
      'Keep the next step explicit.',
      'not, after a lens handoff — the auth replay resets the page.',
    ]) {
      expect(TAG.test(row), row).toBe(false);
    }
  });
});

describe('the transcript wraps at the width it draws', () => {
  // The audit's HUD-03/WIRE-03 pair: drawBrainstormTranscript counted rows
  // at 548px (576 - HUD_EDGE*2) but WRAPPED them at a literal 504 — a
  // second copy of a number from the old narrow column. Every row broke
  // ~44px early (the dead right margin the wearer keeps pointing at) and
  // the scroll arithmetic disagreed with the rows actually drawn.
  it('brainstormTranscriptLines at the drawn width matches the row count', () => {
    const measure = (value: string) => value.length * 5.18;
    const width = 576 - 14 * 2;
    const transcript = [
      'CODEX > ' + 'a long position statement about caching '.repeat(4),
      'CLAUDE > shorter reply',
    ].join('\n');
    const total = brainstormTranscriptRowCount(transcript, width, measure);
    const lines = brainstormTranscriptLines(transcript, width, 40, measure, 0);
    expect(lines.length).toBe(total);
  });

  it('no drawn row is narrower than the column minus one word', () => {
    // A wrapper feeding a narrower width than the draw shows up as rows
    // that all end well short of the column. With one width, a row only
    // ends early because the NEXT word would not fit.
    const measure = (value: string) => value.length * 5.18;
    const width = 576 - 14 * 2;
    const words = 'reasonably sized words repeated over and over again ';
    const lines = brainstormTranscriptLines(
      'CODEX > ' + words.repeat(6), width, 40, measure, 0,
    );
    for (let index = 0; index < lines.length - 1; index += 1) {
      const row = lines[index]!;
      const next = (lines[index + 1] ?? '').split(' ')[0] ?? '';
      expect(measure(row + ' ' + next)).toBeGreaterThan(width);
    }
  });
});

describe('the footer never names a gesture the router will not perform', () => {
  // HUD-08 (audit blocker): a BUSY lane holding a pending approval reports
  // state 'running', and there handleSwipe DENIES the tool. The old hint
  // said 'swipe: scroll' — a reading gesture that actually refused a tool.
  const base = {
    screen: 'agent_terminal' as const,
    state: 'running' as const,
    noToolsChecked: true,
    acceptanceArmed: false,
    // Two lanes: the tap only cycles where there is somewhere to cycle to.
    collaborators: ['work·run', 'hermes·idle'],
  };

  it('a busy lane with a pending approval names approve/deny, not scroll', () => {
    const hint = brainstormActionHint({ ...base, approvals: 2 });
    expect(hint).toContain('swipe: deny');
    expect(hint).not.toContain('swipe: scroll');
  });

  it('a busy lane with no approval still invites scrolling', () => {
    const hint = brainstormActionHint({ ...base, approvals: 0 });
    expect(hint).toContain('swipe: scroll');
  });
});

describe('the spine\'s left slot', () => {
  /** drawStatusSpine with the icon and fitText helpers stubbed, so the assertion
   * is about the slot's TEXT and not about canvas image compositing. */
  function shell(showTime: boolean): string[] {
    const texts: string[] = [];
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      measureText: (value: string) => ({ width: value.length * 5.18 }),
      save() {}, restore() {}, beginPath() {}, stroke() {},
      moveTo() {}, lineTo() {}, arcTo() {}, closePath() {},
      fillText(text: string) { texts.push(text); },
    } as unknown as CanvasRenderingContext2D;
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      drawIcon(): void;
      fitText(c: CanvasRenderingContext2D, t: string): void;
      drawStatusSpine(
        c: CanvasRenderingContext2D, s: string, m: boolean, i: unknown,
        showTime: boolean,
      ): void;
    };
    stub.drawIcon = () => {};
    stub.fitText = (c, t) => { c.fillText(t, 0, 0); };
    stub.drawStatusSpine(
      context, '9:41  JOAT ready  bat 87%', false, {} as unknown, showTime,
    );
    return texts;
  }

  it('carries the clock on a feature page and the DATE on the overview', () => {
    // The dashboard already prints the time at full size, so its spine
    // yields the small clock — and that slot would otherwise sit empty,
    // which is why the date used to be stacked on top of the big clock.
    expect(shell(true)[0]).toBe('9:41');
    expect(shell(false)[0]).toBe(HudCanvas.hudDateLabel(new Date()));
    // Never both: one screen must not set the same fact twice.
    expect(shell(false)).not.toContain('9:41');
  });
});

describe('the compact JOAT banner uses the existing canvas font', () => {
  const SEAM = 288;

  function shell(status: string): { texts: Array<{ text: string; x: number; font: string }>; icons: number[] } {
    const texts: Array<{ text: string; x: number; font: string }> = [];
    const icons: number[] = [];
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      measureText: (value: string) => ({ width: value.length * 5.18 }),
      save() {}, restore() {}, beginPath() {}, stroke() {},
      moveTo() {}, lineTo() {}, arcTo() {}, closePath() {},
      fillText(this: {font: string}, text: string, x: number) { texts.push({ text, x, font: this.font }); },
    } as unknown as CanvasRenderingContext2D;
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      drawIcon(c: unknown, i: unknown, x: number): void;
      drawStatusSpine(
        c: CanvasRenderingContext2D, s: string, m: boolean, i: unknown,
        showTime: boolean,
      ): void;
    };
    stub.drawIcon = (_c, _i, x) => { icons.push(x); };
    stub.drawStatusSpine(context, status, true, {} as unknown, true);
    return { texts, icons };
  }

  it('paints the state word in the compact banner and keeps the mic east of the seam', () => {
    const { texts, icons } = shell('9:41  JOAT hearing you…  bat 87%');
    expect(texts.map((entry) => entry.text)).toEqual(['9:41', 'JOAT · HEARING YOU…', 'ON', '87%']);
    const label = texts.find((entry) => entry.text === 'JOAT · HEARING YOU…');
    expect(label?.font).toContain('10px');
    expect(label?.font).toContain('SF Mono');
    const spineClock = texts.find((entry) => entry.text === '9:41');
    expect(spineClock?.font).toContain('12px');
    expect(texts.filter((entry) => entry !== spineClock)
      .every((entry) => entry.font === label?.font)).toBe(true);
    expect((label?.x ?? 0) + 'JOAT · HEARING YOU…'.length * 6).toBeLessThan(SEAM);
    const mic = texts.find((entry) => entry.text === 'ON');
    expect(mic?.x).toBeGreaterThanOrEqual(SEAM);
    const [micIcon, batteryIcon] = icons;
    expect(micIcon).toBeGreaterThanOrEqual(SEAM);
    expect(batteryIcon).toBeGreaterThanOrEqual(SEAM);
    // Clock and changing label remain separate inside the top-left tile.
    const clock = texts.find((entry) => entry.text === '9:41');
    expect((clock?.x ?? 0) + '9:41'.length * 7.2).toBeLessThan(SPINE_LABEL_BOX.x);
  });

  const overview = (status: string): HudVisualFrame => ({
    kind: 'overview', status, micOn: false, mainBrightness: 4,
    main: '', context: '', tray: '', hint: '',
  });
  const list = (status: string, title: string): HudVisualFrame => ({
    kind: 'list', status, micOn: false, title, items: [], selectable: false,
    selectedIndex: 0, hint: '',
  });

  it('hudSpineLabel names the link state at home and the page elsewhere', () => {
    expect(HudCanvas.hudSpineLabel(overview('9:41  JOAT ready  bat 87%'))).toBe('JOAT · READY');
    expect(HudCanvas.hudSpineLabel(overview('9:41  JOAT hearing you…'))).toBe('JOAT · HEARING YOU…');
    expect(HudCanvas.hudSpineLabel(list('9:41  JOAT ready', 'NEWS'))).toBe('NEWS');
    expect(HudCanvas.hudSpineLabel(list('9:41 JOAT mic off', 'NEWS'))).toBe('NEWS');
    expect(HudCanvas.hudSpineLabel(overview('9:41 JOAT mic off'))).toBe('JOAT');
    // A degraded link outranks any page name.
    expect(HudCanvas.hudSpineLabel(list('9:41  JOAT connecting', 'NEWS'))).toBe('JOAT · CONNECTING');
  });
});

describe('the resting overview wears one footer on every tab', () => {
  // The footer is centred on the tile seam, so a footer that differed
  // between the notifications tab and the other three made a swipe repaint
  // the bottom-left tile too: three image writes where the rail and the
  // pane need two.
  function overviewFooter(rail: boolean): string {
    const texts: string[] = [];
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      lineWidth: 1, strokeStyle: '', globalAlpha: 1,
      measureText: (value: string) => ({ width: value.length * 5.18 }),
      save() {}, restore() {}, beginPath() {}, stroke() {}, fill() {},
      moveTo() {}, lineTo() {}, arcTo() {}, closePath() {}, fillRect() {},
      fillText(text: string) { texts.push(text); },
    } as unknown as CanvasRenderingContext2D;
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      drawIcon(): void;
      drawTabRail(): void;
      drawPanelPane(): void;
      drawOverview(c: CanvasRenderingContext2D, frame: unknown, icons: unknown): void;
    };
    stub.drawIcon = () => {};
    stub.drawTabRail = () => {};
    stub.drawPanelPane = () => {};
    stub.drawOverview(context, {
      status: '9:41  JOAT ready  bat 87%',
      main: 'Nothing new.',
      mainBrightness: 4,
      context: '',
      tray: '',
      hint: 'tap: open · swipe: tabs · hold: mic · “JOAT …”',
      ...(rail ? { rail: { activeTab: 'notifications' } } : {}),
    }, {});
    const footer = texts[texts.length - 1];
    if (footer === undefined) throw new Error('no footer drawn');
    return footer;
  }

  it('draws the shared rail hint on the notifications tab, not the chrome hint', () => {
    expect(overviewFooter(true)).toBe(HudCanvas.PANEL_OVERVIEW_HINT);
  });

  it('keeps the chrome hint where there is no rail to swipe', () => {
    expect(overviewFooter(false)).toBe('tap: open · swipe: tabs · hold: mic · “JOAT …”');
  });
});

describe('the scrollback marker keeps the two-tone split', () => {
  // S6 from the audit's counter-pass: windowHudTailLines prepends '…' to
  // the first visible row whenever older rows are hidden, and '…CODEX > '
  // matched no tag — so the topmost row drew wholly bright on every
  // scrolled frame, the one row a scrolling reader looks at first.
  function record(): { context: CanvasRenderingContext2D; calls: Array<{ style: string; text: string }> } {
    const calls: Array<{ style: string; text: string }> = [];
    const context = {
      fillStyle: '',
      font: '',
      textAlign: 'left',
      measureText: (value: string) => ({ width: value.length * 5.18 }),
      fillText(this: { fillStyle: string }, text: string) {
        calls.push({ style: String(this.fillStyle), text });
      },
    } as unknown as CanvasRenderingContext2D;
    return { context, calls };
  }

  it('the marked first row still draws a dim tag and bright words', () => {
    const { context, calls } = record();
    const renderer = Object.create(
      HudCanvasRenderer.prototype,
    ) as HudCanvasRenderer;
    const transcript = Array.from(
      { length: 20 }, (_v, index) => `CODEX > position number ${index}`,
    ).join('\n');
    (renderer as never as {
      drawBrainstormTranscript(
        c: CanvasRenderingContext2D, t: string, x: number, y: number,
        m: number, s: { offset: number; rows: number; visible: number },
      ): void;
    }).drawBrainstormTranscript(
      context, transcript, 14, 82, 13,
      { offset: 0, rows: 0, visible: 0 },
    );
    // The FIRST ROW must produce two draw calls: the dim '…CODEX ' tag,
    // then the bright words — not one flat call of the whole line. (An
    // earlier version compared call 0 to call 1 without checking they
    // belonged to the same row, and the unfixed code passed it: row one
    // drew flat-bright in one call and row TWO's dim tag supplied the
    // differing style. A mutation check caught the vacuity.)
    const first = calls[0]!;
    const second = calls[1]!;
    expect(first.text).toBe('…CODEX ');
    expect(second.text.startsWith('position')).toBe(true);
    expect(first.style).not.toBe(second.style);
  });
});


describe('reader width respects the active tab rail', () => {
  it.each(['NEWS · ARTICLE', 'SEARCH · RESULT'])('%s keeps its leading letters clear of the rail', (title) => {
    const text: Array<{value: string; x: number; y: number}> = [];
    const state: Record<string, unknown> = {
      font: '', textAlign: 'left',
      measureText: (value: string) => ({width: value.length * 8.4}),
      fillText: (value: string, x: number, y: number) => text.push({value, x, y}),
    };
    const context = new Proxy(state, {get: (target, key) => target[String(key)] ?? (() => undefined)});
    const renderer = Object.create(HudCanvasRenderer.prototype) as any;
    renderer.drawIcon = () => undefined;
    renderer.drawList(context, {
      kind: 'list', title, items: ['Story source', 'Opening letters must remain visible.'],
      hint: 'left x2: back', selectable: false, selectedIndex: 0,
      rail: {activeTab: 'news'},
    }, {});
    const body = text.filter(({y}) => y >= 78 && y <= 245);
    expect(body.length).toBeGreaterThan(0);
    expect(body.every(({x}) => x >= 36)).toBe(true);
  });
});

describe('exact prepared readers across feature boundaries', () => {
  function readerFixture() {
    const painted: Array<{text: string; x: number; y: number; width: number}> = [];
    const state: Record<string, unknown> = {font: ''};
    const width = (text: string) => Array.from(text).reduce((sum, char) =>
      sum + (/\p{Script=Han}/u.test(char) || char.codePointAt(0)! > 0xffff ? 2 : 1), 0) * 8.4;
    Object.assign(state, {
      measureText: (text: string) => ({width: width(text)}),
      fillText: (text: string, x: number, y: number) => painted.push({text, x, y, width: width(text)}),
    });
    const context = new Proxy(state, {get: (target, key) => target[String(key)] ?? (() => undefined)});
    const renderer = Object.create(HudCanvasRenderer.prototype) as any;
    renderer.drawIcon = () => undefined;
    return {painted, context, renderer};
  }

  it.each(['MESSAGES · MESSAGE', 'NEWS · ARTICLE', 'SEARCH · RESULT', 'QUICKNOTES · NOTE', 'QUICKNOTES · DETAIL'])(
    '%s paints every prepared row at full width without merging or shortening it', (title) => {
      const {painted, context, renderer} = readerFixture();
      const panel = title.startsWith('NEWS') || title.startsWith('SEARCH');
      const body = ['  leading  spaces\r\n', '\r\n', '漢'.repeat(31), '🙂'.repeat(31),
        'x'.repeat(62), 'e\u0301 👩\u200d💻', '\taligned\r', 'middle', 'penultimate\n', 'FINAL  '];
      renderer.drawList(context, {kind: 'list', title, items: ['1/2 · Source', ...body],
        selectedIndex: 0, selectable: false, hint: 'down: next · left x2: back',
        ...(panel ? {rail: {activeTab: title.startsWith('NEWS') ? 'news' : 'search'}} : {}),
      }, {});
      const bodyPainted = painted.filter(({y}) => y >= 84 && y <= 237);
      expect(bodyPainted.map(({text}) => text)).toEqual(body.map(line =>
        line.replace(/\r\n$|[\r\n]$/u, '').replace(/\t/g, '        ')));
      expect(bodyPainted.every(({x, width}) => x === (panel ? 36 : 14) && x + width <= 562)).toBe(true);
      expect(painted.find(({y}) => y === 58)?.text).toBe('1/2 · Source');
      expect(painted.find(({y}) => y === 267)?.text).toBe('down: next · left x2: back');
    },
  );

  it('rejects an overfull prepared page instead of replacing unseen content with an ellipsis', () => {
    const {context, renderer} = readerFixture();
    expect(() => renderer.drawList(context, {kind: 'list', title: 'MESSAGES · MESSAGE',
      items: ['1/1 · Source', ...Array.from({length: 11}, (_, i) => `row ${i}`)],
      selectedIndex: 0, selectable: false, hint: 'left x2: back',
    }, {})).toThrow(/reader.*10.*rows/i);
  });
});


describe('terminal conversation footer transitions', () => {
  it('repaints speak after an answer and keeps Back while reading older output', () => {
    const painted: string[] = [];
    const context = new Proxy({
      fillText: (text: string) => painted.push(text),
      measureText: (text: string) => ({width: text.length * 4}),
    }, {get: (target, key) => Reflect.get(target, key) ?? (() => undefined)});
    const renderer = Object.create(HudCanvasRenderer.prototype) as any;
    renderer.drawIcon = () => undefined;
    renderer.drawBrainstormTranscript = () => undefined;
    renderer.fitText = (ctx: any, text: string) => ctx.fillText(text);
    let offset = 0;
    renderer.transcriptScrollFor = () => ({offset});
    const model = {screen: 'agent_terminal', state: 'running',
      topic: 'OpenClaw · running', collaborators: [], activity: 'Answer', noToolsChecked: false};
    renderer.drawBrainstorm(context, model, {});
    expect(painted[painted.length - 1]).toContain('terminal cancel');
    renderer.drawBrainstorm(context, {...model, state: 'ready', topic: 'OpenClaw · ready'}, {});
    expect(painted[painted.length - 1]).toContain('tap: speak');
    expect(painted[painted.length - 1]).not.toContain('terminal cancel');
    offset = 3;
    renderer.drawBrainstorm(context, {...model, state: 'ready'}, {});
    expect(painted[painted.length - 1]).toContain('double-click: back');
    expect(painted[painted.length - 1]).not.toContain('menu');
  });
});

describe('inline terminal composer rendering', () => {
  it('retains the conversation and complete question while showing the listening action', () => {
    const painted: Array<{text: string; y: number}> = [];
    const context = new Proxy({
      fillText: (text: string, _x: number, y: number) => painted.push({text, y}),
      measureText: (text: string) => ({width: text.length * 6}),
    }, {get: (target, key) => Reflect.get(target, key) ?? (() => undefined)});
    const renderer = Object.create(HudCanvasRenderer.prototype) as any;
    renderer.drawIcon = () => undefined;
    renderer.fitText = (ctx: any, text: string, x: number, y: number) => ctx.fillText(text, x, y);
    renderer.transcriptScrollFor = () => ({offset:0});
    renderer.drawBrainstorm(context, {screen:'agent_terminal', state:'ready', topic:'Hermes · ready',
      collaborators: [], activity:'', conversationId:'hermes-conversation',
      messages:[{id:'old-reply',role:'assistant',text:'Previous answer remains visible.'}],
      terminalPrompt:{id:'prompt',question:'Reply to Hermes.'}, inputRequired:true}, {});
    expect(painted.some(row => row.text === 'AGENT > Previous answer remains visible.')).toBe(true);
    expect(painted.some(row => row.text === 'Reply to Hermes.')).toBe(true);
    expect(painted.some(row => row.text.includes('REPLY NEEDED'))).toBe(true);
    expect(painted.some(row => row.text === 'READY')).toBe(false);
    expect(painted[painted.length-1]?.text).toContain('Listening');
    expect(painted.filter(row => row.text === 'Reply to Hermes.').every(row => row.y < 257)).toBe(true);
  });
});

it('keeps uncertain owned tasks distinct from connection failure', () => {
  const model = {screen: 'agent_terminal' as const, state:'blocked' as const,
    topic:'Hermes · check task', taskRecovery:true};
  const layout = agentTerminalTranscriptLayout(model);
  expect(layout.summary).toBe('HERMES > CHECK TASK');
  expect(layout.showRecoveryDetails).toBe(false);
  expect(brainstormActionHint(model)).toBe('tap: check task status · double-click: back');
});
