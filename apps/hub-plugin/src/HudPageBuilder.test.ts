import { describe, expect, it } from 'vitest';
import { utf8ByteLength, validateEvenHubPageContainer } from '@evenrealities/even_hub_sdk';
import { getTextWidth, measureTextWrap, pxTruncate } from '@evenrealities/pretext';
import { LIST_ROW_MAX_UTF8_BYTES, clampUtf8Bytes, fitHeader, HudPageBuilder } from './HudPageBuilder';
import { CANVAS, CAPABILITIES, CAPABILITY_MENU_ID_BASE, ContainerId } from './types';

type Page = {
  containerTotalNum?: number;
  textObject?: Array<{
    containerID?: number;
    containerName?: string;
    isEventCapture?: number;
    zOrderIndex?: number;
    xPosition?: number;
    yPosition?: number;
    width?: number;
    height?: number;
    borderWidth?: number;
    textColor?: number;
    content?: string;
  }>;
  listObject?: Array<{
    containerID?: number;
    containerName?: string;
    isEventCapture?: number;
    zOrderIndex?: number;
    xPosition?: number;
    yPosition?: number;
    width?: number;
    height?: number;
    borderWidth?: number;
    borderColor?: number;
    itemContainer?: { itemCount?: number; itemName?: string[] };
  }>;
  imageObject?: Array<{
    containerID?: number;
    containerName?: string;
    isEventCapture?: number;
    zOrderIndex?: number;
  }>;
  menuObject?: { menuItems?: Array<{ itemID?: number; itemName?: string }> };
};

function containers(page: Page) {
  return [...(page.textObject ?? []), ...(page.listObject ?? []), ...(page.imageObject ?? [])];
}

const builder = new HudPageBuilder();
const LAYOUTS: ReadonlyArray<[string, Page]> = [
  ['compatibility', builder.compatibilityLayout() as Page],
  ['dashboard-compatibility', builder.dashboardCompatibilityLayout() as Page],
  ['default', builder.defaultLayout() as Page],
  ['list', builder.listLayout(['a', 'b', 'c']) as Page],
  ['confirmation', builder.confirmationLayout('Send reply?') as Page],
  ['visual-startup', builder.visualStartup() as Page],
  ['visual', builder.visualLayout() as Page],
  ['visual-compatibility', builder.visualCompatibilityLayout() as Page],
  ['assistant-conversation', builder.assistantConversationLayout({
    cardTop: 128,
    cardHeight: 132,
    transcriptTop: 140,
    transcriptHeight: 81,
    footerTop: 247,
    dimText: 'Tell me a joke.\n \n ',
    brightText: ' \nWhy did the byte cross the bus?\n ',
    footerText: '■ □',
  }) as Page],
];

describe('HudPageBuilder invariants', () => {
  // Schema regression only. The packaged phone has already shown that SDK
  // validation and simulator acceptance do not prove native-host acceptance.
  it.each(LAYOUTS)('%s: passes the installed SDK page validator', (_name, page) => {
    expect(validateEvenHubPageContainer(page)).toEqual({ valid: true });
  });

  it('makes the one-shot startup an image-first hardware surface', () => {
    const startup = builder.visualStartup() as Page;
    expect(startup.containerTotalNum).toBe(5);
    expect(startup.menuObject?.menuItems).toContainEqual({
      itemID: CAPABILITY_MENU_ID_BASE + CAPABILITIES.indexOf('Planning'),
      itemName: 'Planning',
    });
    expect(startup.imageObject).toHaveLength(4);
    expect(startup.textObject).toHaveLength(1);
    expect(startup.textObject?.[0]).toMatchObject({
      isEventCapture: 1,
      content: ' ',
      textColor: 0,
      zOrderIndex: 1,
    });
    expect(startup.imageObject?.every(
      (image) => (image.zOrderIndex ?? 0) > (startup.textObject?.[0]?.zOrderIndex ?? 0),
    )).toBe(true);
  });

  it('keeps native lettering out of the authored compact banner', () => {
    for (const page of [builder.visualStartup() as Page, builder.visualLayout() as Page]) {
      expect(page.textObject).toHaveLength(1);
      expect(page.textObject?.[0]).toMatchObject({content: ' ', isEventCapture: 1});
    }
  });

  it('builds the conversation as Even AI builds it: one bordered, bottom-anchored card', () => {
    // The reference is Even's own product-page demo (frame-measured
    // 2026-09-24): a single hairline box, the wearer's question dim above
    // the bright answer, the box growing upward from a fixed bottom, the
    // state glyph centred ON the bottom border — and no scrollbar; older
    // lines simply leave through the top of the card.
    const page = builder.assistantConversationLayout({
      cardTop: 128,
      cardHeight: 132,
      transcriptTop: 140,
      transcriptHeight: 81,
      footerTop: 247, // cardBottom 260 - half a 27px line: straddles the border
      dimText: 'Tell me a joke.\n \n ',
      brightText: ' \nWhy did the byte cross the bus?\n ',
      footerText: '■ □',
    }) as Page;
    const byName = (name: string) => page.textObject?.find((entry) => entry.containerName === name);

    expect(page.imageObject).toBeUndefined();
    expect(page.textObject).toHaveLength(4);
    expect(page.textObject?.find((entry) => entry.containerName === 'scroll-position')).toBeUndefined();
    expect(byName('conversation')).toMatchObject({
      yPosition: 128,
      height: 132,
      borderWidth: 1,
      isEventCapture: 1,
    });
    expect(byName('wearer-turns')).toMatchObject({ textColor: 2, content: expect.stringContaining('joke') });
    expect(byName('joat-turns')).toMatchObject({ textColor: 4, content: expect.stringContaining('byte') });
    // The glyph line straddles the card's bottom edge, exactly as passed.
    expect(byName('generation')).toMatchObject({ content: '■ □', yPosition: 247 });

    const frame = byName('conversation')!;
    const transcript = byName('wearer-turns')!;
    const footer = byName('generation')!;
    expect((transcript.yPosition ?? 0) + (transcript.height ?? 0))
      .toBeLessThanOrEqual(footer.yPosition ?? 0);
    // Straddling: the footer line starts above the border and ends below it.
    expect(footer.yPosition ?? 0).toBeLessThan((frame.yPosition ?? 0) + (frame.height ?? 0));
    expect((footer.yPosition ?? 0) + (footer.height ?? 0))
      .toBeGreaterThan((frame.yPosition ?? 0) + (frame.height ?? 0));
  });

  it('keeps every visual container name within the packaged-host limit', () => {
    const visual = builder.visualLayout() as Page;
    const names = containers(visual).map((container) => container.containerName);
    expect(names.every((name) => typeof name === 'string' && name.length <= 16)).toBe(true);
    expect(new Set(names).size).toBe(names.length);
  });

  it('makes menu-free compatibility durable across every later rebuild shape', () => {
    const compatible = builder.withContextualMenu(false);
    const pages: Page[] = [
      compatible.defaultLayout() as Page,
      compatible.listLayout(['a']) as Page,
      compatible.confirmationLayout('confirm') as Page,
      compatible.visualLayout() as Page,
    ];
    expect(pages.every((page) => page.menuObject === undefined)).toBe(true);
    // Settings lives on the phone and has no firmware menu row; every
    // other capability is offered.
    expect((builder.defaultLayout() as Page).menuObject?.menuItems)
      .toHaveLength(CAPABILITIES.filter((name) => name !== 'Settings').length);
  });

  it('keeps the contextual escape available on every image-first rebuild', () => {
    const visual = builder.visualLayout() as Page;
    // The firmware menu offers exactly the wearer's capability rows — one
    // menu, reached by the platform's own gesture, costing zero image tiles.
    // Settings is phone-owned and has no row, but the ids keep their
    // original index so no surviving action id moves when it drops out.
    const expected = CAPABILITIES.map((name, index) => ({
      itemID: CAPABILITY_MENU_ID_BASE + index,
      itemName: name,
    })).filter(({ itemName }) => itemName !== 'Settings');
    expect(expected).toHaveLength(CAPABILITIES.length - 1);
    expect(visual.menuObject?.menuItems).toEqual(expected);
    expect((builder.visualCompatibilityLayout() as Page).menuObject?.menuItems).toEqual(expected);
  });

  it('still routes a retired id from a host showing a cached menu', () => {
    // The ids outlive the rows: EventRouter must keep handling Planning,
    // Repeat and Settings even though this menu no longer offers them.
    expect(HudPageBuilder.MENU_PLANNING).toBe(5);
    expect(HudPageBuilder.MENU_REPEAT).toBe(1);
    expect(HudPageBuilder.MENU_SETTINGS).toBe(4);
  });

  it.each(LAYOUTS)('%s: exactly one container captures events', (_name, page) => {
    const capturing = containers(page).filter((c) => c.isEventCapture === 1);
    expect(capturing).toHaveLength(1);
  });

  it.each(LAYOUTS)('%s: zOrderIndex is set on every container and unique', (_name, page) => {
    const all = containers(page);
    const z = all.map((c) => c.zOrderIndex);
    if (z.every((v) => v === undefined)) return;
    expect(z.every((v) => typeof v === 'number')).toBe(true);
    expect(new Set(z).size).toBe(all.length);
  });

  it.each(LAYOUTS)('%s: containerTotalNum matches the declared containers', (_name, page) => {
    expect(page.containerTotalNum).toBe(containers(page).length);
  });

  // The bug this file exists to prevent: the list draws, but capture stays on
  // the main TEXT container, so the firmware never moves the selection and
  // never emits listEvent — "menu shows but won't navigate".
  it('keeps every firmware list row under the host\'s 63-byte cap, on a code-point boundary', () => {
    // Measured in the official simulator: a 64-byte row fails the whole
    // rebuild. "·" is two bytes, so a 64-character Messages row overflows.
    const narrow = 'i'.repeat(64);
    const dotted = `You · Read · ${'x'.repeat(51)}`; // 64 chars, 66 bytes
    const page = builder.listLayout([narrow, dotted, 'short']) as Page;
    const names = page.listObject?.[0]?.itemContainer?.itemName ?? [];
    for (const row of names) {
      expect(new TextEncoder().encode(row).length).toBeLessThanOrEqual(LIST_ROW_MAX_UTF8_BYTES);
    }
    expect(names[0]).toBe('i'.repeat(63));
    expect(names[1]?.startsWith('You · Read · ')).toBe(true);
    expect(names[2]).toBe('short');
    expect(clampUtf8Bytes('ab·cd', 3)).toBe('ab');
    expect(clampUtf8Bytes('ab·cd', 4)).toBe('ab·');
  });

  it('list layout: the LIST container is the one that captures', () => {
    const page = builder.listLayout(['a', 'b', 'c']) as Page;
    expect(page.listObject?.[0]?.isEventCapture).toBe(1);
    expect(page.textObject?.find((c) => c.containerID === ContainerId.StatusBar)?.isEventCapture).toBe(0);
  });

  it('list layout: caps at 20 items x 64 chars and fits the canvas', () => {
    const page = builder.listLayout(
      Array.from({ length: 25 }, (_, i) => 'x'.repeat(80) + i),
    ) as unknown as {
      listObject: Array<{
        yPosition: number;
        height: number;
        itemContainer: { itemCount: number; itemName: string[] };
      }>;
    };
    const list = page.listObject[0]!;
    expect(list.itemContainer.itemCount).toBe(20);
    expect(list.itemContainer.itemName).toHaveLength(20);
    expect(list.itemContainer.itemName.every((s) => s.length <= 64)).toBe(true);
    expect(list.yPosition + list.height).toBeLessThanOrEqual(CANVAS.height);
  });

  it('list layout: every row is within the firmware 63-BYTE limit, not 64 chars', () => {
    // A 66-char headline with two curly quotes is 70 bytes; the old 64-char
    // pre-cut let it through and the firmware rejected the whole page —
    // News expanded to nothing (2026-09-22).
    const headline = "[20m] Trump tells U.N. he could ‘annihilate’ Iran but expects a deal · NBC";
    const page = builder.listLayout([headline]) as unknown as {
      listObject: Array<{ itemContainer: { itemName: string[] } }>;
    };
    for (const row of page.listObject[0]!.itemContainer.itemName) {
      expect(utf8ByteLength(row)).toBeLessThanOrEqual(63);
    }
  });

  it('text overview: a full News card stays within the firmware 999-byte pane limit', () => {
    const rows = Array.from({ length: 20 }, (_, i) => `[${i + 1}m] A headline long enough to matter, number ${i} · Source`);
    const page = builder.overviewTextLayout({
      status: '11:33 · MIC ON · bat 100%', clock: '11:33', date: 'Tue 22 Sep', ambient: '',
      tabs: [{ label: 'NOTIFICATIONS', active: false }, { label: 'NEWS', active: true }],
      paneTitle: 'NEWS', paneBody: rows.join('\n'), hint: 'double-click: menu · long-press: mic',
    }) as unknown as { textObject: Array<{ containerName: string; content: string }> };
    expect(utf8ByteLength(rows.join('\n'))).toBeGreaterThan(999); // the case that failed live
    for (const t of page.textObject) expect(utf8ByteLength(t.content)).toBeLessThanOrEqual(999);
    expect(validateEvenHubPageContainer(page as never).valid).toBe(true);
  });

  it('text overview: the tab rail captures swipes; the pane is a seven-row, one-line-per-row preview', () => {
    // With the pane capturing, an overflowing News card swallowed every
    // swipe (the firmware scrolled the card instead of reporting the
    // swipe) and the rail never moved — observed 2026-09-22.
    const rows = Array.from({ length: 12 }, (_, i) => `[${i + 1}m] A headline that is far too long to fit on one pane line of the right column · Source`);
    const page = builder.overviewTextLayout({
      status: 's', clock: '11:33', date: 'Tue', ambient: '',
      tabs: [{ label: 'NEWS', active: true }],
      paneTitle: 'NEWS', paneBody: rows.join('\n'), hint: 'double-click: menu',
    }) as unknown as { textObject: Array<{ containerName: string; content: string; isEventCapture: number; width: number }> };
    const byName = Object.fromEntries(page.textObject.map((t) => [t.containerName, t]));
    expect(byName['tabs']!.isEventCapture).toBe(1);
    expect(byName['pane']!.isEventCapture).toBe(0);
    expect(page.textObject.filter((t) => t.isEventCapture === 1)).toHaveLength(1);
    const preview = byName['pane']!.content.split('\n');
    expect(preview).toHaveLength(7);
    for (const line of preview) expect(pxTruncate(line, byName['pane']!.width - 4)).toBe(line);
  });

  it('text overview: ten firmware lines tile the whole 576x288 canvas, nothing overflows', () => {
    const page = builder.overviewTextLayout({
      status: 'Sun 10/5 · MIC OFF · bat 100%', clock: '9:52', date: 'Sun 10/5', ambient: '72° Clear',
      extra: '3 unread',
      tabs: [
        { label: 'NOTIFICATIONS', active: true }, { label: 'NEWS', active: false },
        { label: 'POMODORO', active: false }, { label: 'SEARCH', active: false },
      ],
      paneTitle: 'NOTIFICATIONS',
      paneBody: Array.from({ length: 9 }, (_, i) => `Row ${i + 1} · a notification long enough to need the whole pane width`).join('\n'),
      hint: 'tap: open · swipe: tabs',
    }) as unknown as { textObject: Array<{
      containerName: string; content: string; xPosition: number; yPosition: number;
      width: number; height: number; paddingLength: number; borderWidth: number;
    }> };
    const by = Object.fromEntries(page.textObject.map((t) => [t.containerName, t]));
    // Edge to edge, top to bottom: no unused band at the bottom or right.
    expect(Math.max(...page.textObject.map((t) => t.yPosition + t.height))).toBe(288);
    expect(Math.max(...page.textObject.map((t) => t.xPosition + t.width))).toBe(576);
    expect(by['status']!.yPosition).toBe(0);
    expect(by['hint']!.yPosition + by['hint']!.height).toBe(288);
    // Every container's text fits its inner box: no firmware scrollbar.
    for (const t of page.textObject.filter((c) => c.containerName !== 'divider')) {
      const inset = t.paddingLength + t.borderWidth;
      const inner = t.width - 2 * inset;
      const lines = Math.floor((t.height - 2 * inset) / 27);
      expect(measureTextWrap(t.content, inner).lineCount, t.containerName).toBeLessThanOrEqual(lines);
    }
    // The divider is a bordered 2px rule carrying a single space, the full
    // height of the middle band.
    expect(by['divider']).toMatchObject({ width: 2, borderWidth: 1, content: ' ', height: 216 });
    // The time is printed once, in the clock column, not in the status line too.
    expect(by['clock']!.content.split('\n')).toEqual(['9:52', '72° Clear', '3 unread']);
    expect(by['pane']!.content.split('\n')).toHaveLength(7);
  });

  it('text overview: long Mac text is cut to its band (no scrollbar) and rail labels share one x', () => {
    const page = builder.overviewTextLayout({
      status: 'Wednesday, September 30 · MIC ON · bat 7% · charging · glasses L 12% R 9% · reconnecting to Mac',
      clock: '12:59', date: 'Wed, Sep 30', ambient: '104° Thunderstorms and heavy rain', extra: '128 unread',
      tabs: [
        { label: 'NOTIFICATIONS', active: false }, { label: 'NEWS', active: true },
        { label: 'POMODORO', active: false }, { label: 'SEARCH', active: false },
      ],
      paneTitle: 'NEWS · BBC World · Woman and three children die in house fire on Merseyside',
      paneBody: 'one',
      hint: 'tap: open article · swipe: tabs · double-click: back · long-press: mute microphone · ring: scroll',
    }) as unknown as { textObject: Array<{
      containerName: string; content: string; width: number; height: number; paddingLength: number; borderWidth: number;
    }> };
    for (const t of page.textObject.filter((c) => c.containerName !== 'divider')) {
      const inset = t.paddingLength + t.borderWidth;
      const lines = Math.floor((t.height - 2 * inset) / 27);
      expect(measureTextWrap(t.content, t.width - 2 * inset).lineCount, t.containerName).toBeLessThanOrEqual(lines);
    }
    // Proportional font: the selected row's '> ' and an unselected row's
    // indent measure the same, so every label starts at the same x.
    const rail = page.textObject.find((t) => t.containerName === 'tabs')!.content.split('\n');
    const indent = (row: string): number => getTextWidth(row.slice(0, row.length - row.trimStart().replace(/^> /, '').length));
    expect(new Set(rail.map(indent)).size).toBe(1);
  });

  it('list layout gives full-focus content a title and quiet bottom action rail', () => {
    const page = builder.listLayout(['Messages', 'Settings'], {
      title: 'CAPABILITIES',
      hint: 'SWIPE move · TAP open · LEFT x2 back',   // authored form
    }) as Page;
    // One header line carries the page's name (the presenter composes the
    // clock · page · mic · battery line in; a bare title is the fallback),
    // and the list starts straight under it: five rows, not four.
    const header = page.textObject?.find((c) => c.containerID === ContainerId.StatusBar);
    const list = page.listObject?.[0];

    const hint = page.textObject?.find((c) => c.containerID === ContainerId.Hint);

    expect(page.containerTotalNum).toBe(3);
    expect(page.textObject?.find((c) => c.containerID === ContainerId.Main)).toBeUndefined();
    expect(header).toMatchObject({ yPosition: 0, height: 35, textColor: 2, content: 'CAPABILITIES' });
    expect(list).toMatchObject({ yPosition: 35, height: 216 });
    expect(hint).toMatchObject({ yPosition: 253, height: 35, textColor: 1 });
    // The builder rewrites a Mac's old left-only wording to the gesture
    // GesturePolicy honours on every source (GesturePolicy.formatHint).
    expect(hint?.content).toBe('SWIPE move · TAP open · double-click back');
    expect((hint?.yPosition ?? 0) + (hint?.height ?? 0)).toBe(CANVAS.height);
  });

  it('fits a long header by shortening the page word, never the mic and battery glyphs', () => {
    const short = '9:41 · NEWS · ● · ▇';
    expect(fitHeader(short)).toBe(short);
    // A word that overruns the 568 px line loses characters to an ellipsis;
    // the clock stays first and the two glyphs stay last.
    const long = `9:41 · ${'MESSAGES · CONVERSATION WITH A VERY LONG NAME'} · ● · ▂ 12%`;
    const fitted = fitHeader(long);
    expect(fitted).not.toBe(long);
    expect(fitted.startsWith('9:41 · MESSAGES · ')).toBe(true);
    expect(fitted.endsWith(' · ● · ▂ 12%')).toBe(true);
    expect(fitted).toContain('…');
    // The layouts apply it: a list's header is never tail-cut.
    const page = builder.listLayout(['a'], { status: long }) as Page;
    expect(page.textObject?.find((c) => c.containerID === ContainerId.StatusBar)?.content).toBe(fitted);
  });

  it('confirmation is a dedicated full-screen decision surface with no list or button chrome', () => {
    const page = builder.confirmationLayout('Send reply to Adele?') as Page;
    const byId = (id: number) => page.textObject?.find((c) => c.containerID === id);

    expect(page.listObject).toBeUndefined();
    expect(page.containerTotalNum).toBe(5);
    expect(byId(ContainerId.Main)).toMatchObject({
      yPosition: 35, height: 144, textColor: 4,
      content: 'CONFIRM ACTION\n\nSend reply to Adele?',
    });
    expect(byId(ContainerId.Context)?.content).toBe('TAP · confirm   SWIPE · cancel');
    expect(byId(ContainerId.Tray)?.content).toBe('');
    expect(byId(ContainerId.Hint)?.content).toBe('8 seconds · timeout cancels');
    expect(page.textObject?.map((container) => container.content).join(' '))
      .not.toMatch(/\b(?:left|right|ring|nod)\b/i);
  });

  it('visual layout tiles the full canvas in front of one hardware-safe gesture layer', () => {
    const page = builder.visualLayout() as unknown as {
      containerTotalNum: number;
      textObject: Array<{
        xPosition: number; yPosition: number; width: number; height: number;
        content: string; textColor: number; isEventCapture: number; zOrderIndex: number;
      }>;
      imageObject: Array<{
        containerID: number; xPosition: number; yPosition: number;
        width: number; height: number; zOrderIndex: number;
      }>;
    };

    expect(page.containerTotalNum).toBe(5);
    expect(page.textObject).toEqual([
      expect.objectContaining({
        xPosition: 0, yPosition: 0, width: 576, height: 288,
        content: ' ', textColor: 0, isEventCapture: 1, zOrderIndex: 1,
      }),
    ]);
    expect(page.imageObject).toEqual([
      expect.objectContaining({ xPosition: 0, yPosition: 0, width: 288, height: 144, zOrderIndex: 2 }),
      expect.objectContaining({ xPosition: 288, yPosition: 0, width: 288, height: 144, zOrderIndex: 3 }),
      expect.objectContaining({ xPosition: 0, yPosition: 144, width: 288, height: 144, zOrderIndex: 4 }),
      expect.objectContaining({ xPosition: 288, yPosition: 144, width: 288, height: 144, zOrderIndex: 5 }),
    ]);
  });

  it('dashboard encodes brightness hierarchy instead of relying on firmware defaults', () => {
    const page = builder.defaultLayout() as Page;
    const byId = (id: number) => page.textObject?.find((c) => c.containerID === id)?.textColor;
    expect(byId(ContainerId.StatusBar)).toBe(2);
    expect(byId(ContainerId.Main)).toBe(4);
    expect(byId(ContainerId.Context)).toBe(3);
    expect(byId(ContainerId.Tray)).toBe(2);
    expect(byId(ContainerId.Hint)).toBe(1);
  });
});
