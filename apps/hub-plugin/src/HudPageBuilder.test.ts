import { describe, expect, it } from 'vitest';
import { utf8ByteLength, validateEvenHubPageContainer } from '@evenrealities/even_hub_sdk';
import { pxTruncate } from '@evenrealities/pretext';
import { HudPageBuilder } from './HudPageBuilder';
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
    dimText: 'Tell me a joke.\n \n ',
    brightText: ' \nWhy did the byte cross the bus?\n ',
    scrollbarText: '│\n█\n│',
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

  it('builds the conversation as one borderless, bottom-anchored native surface', () => {
    const page = builder.assistantConversationLayout({
      cardTop: 128,
      cardHeight: 132,
      transcriptTop: 140,
      transcriptHeight: 81,
      dimText: 'Tell me a joke.\n \n ',
      brightText: ' \nWhy did the byte cross the bus?\n ',
      scrollbarText: '│\n█\n│',
      footerText: '■ □',
    }) as Page;
    const byName = (name: string) => page.textObject?.find((entry) => entry.containerName === name);

    expect(page.imageObject).toBeUndefined();
    expect(page.textObject).toHaveLength(5);
    // borderWidth 0, not 1. The frame container exists to be the page's single
    // isEventCapture owner; the stroke was decoration and the wearer saw it as
    // a hairline box around every answer on real glass.
    //
    // Event capture does NOT require a border. Every one of Even's own
    // templates pairs isEventCapture: 1 with borderWidth: 0 —
    // evenhub-templates asr/src/main.ts, minimal/src/main.ts,
    // text-heavy/src/main.ts, and image/src/main.ts, whose transparent
    // full-screen 'eventLayer' is exactly this pattern. Do not restore the
    // border to "keep events working"; that is not what makes them work.
    expect(byName('conversation')).toMatchObject({
      yPosition: 128,
      height: 132,
      borderWidth: 0,
      isEventCapture: 1,
    });
    expect(byName('wearer-turns')).toMatchObject({ textColor: 2, content: expect.stringContaining('joke') });
    expect(byName('joat-turns')).toMatchObject({ textColor: 4, content: expect.stringContaining('byte') });
    expect(byName('generation')).toMatchObject({ content: '■ □' });
    expect(byName('scroll-position')).toMatchObject({ content: '│\n█\n│' });

    const frame = byName('conversation')!;
    const transcript = byName('wearer-turns')!;
    const footer = byName('generation')!;
    expect((transcript.yPosition ?? 0) + (transcript.height ?? 0))
      .toBeLessThanOrEqual(footer.yPosition ?? 0);
    expect((footer.yPosition ?? 0) + (footer.height ?? 0))
      .toBeLessThan((frame.yPosition ?? 0) + (frame.height ?? 0));
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
    expect((builder.defaultLayout() as Page).menuObject?.menuItems)
      .toHaveLength(CAPABILITIES.length);
  });

  it('keeps the contextual escape available on every image-first rebuild', () => {
    const visual = builder.visualLayout() as Page;
    // The firmware menu offers exactly the wearer's capability rows — one
    // menu, reached by the platform's own gesture, costing zero image tiles.
    const expected = CAPABILITIES.map((name, index) => ({
      itemID: CAPABILITY_MENU_ID_BASE + index,
      itemName: name,
    }));
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
  it('list layout: the LIST container is the one that captures', () => {
    const page = builder.listLayout(['a', 'b', 'c']) as Page;
    expect(page.listObject?.[0]?.isEventCapture).toBe(1);
    expect(page.textObject?.find((c) => c.containerID === ContainerId.Main)?.isEventCapture).toBe(0);
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

  it('text overview: the tab rail captures swipes; the pane is a six-row, one-line-per-row preview', () => {
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
    expect(preview).toHaveLength(6);
    for (const line of preview) expect(pxTruncate(line, byName['pane']!.width - 8)).toBe(line);
  });

  it('reader layout: an opened article is four firmware text containers, body captures, rows rejoined', () => {
    const page = builder.listLayout(
      ['1/5 · BBC · 16m · Woman and three children die', 'A woman and three children have died in a house fire on ', 'Merseyside.\n', 'Six fire engines were at the scene.'],
      { title: 'NEWS · ARTICLE', hint: 'down more · left x2: back', nativeReaderPage: true },
    ) as unknown as {
      containerTotalNum: number;
      textObject: Array<{ containerID: number; containerName: string; content: string; isEventCapture: number; zOrderIndex: number; yPosition: number; height: number }>;
      listObject?: unknown[];
      imageObject?: unknown[];
    };
    expect(page.containerTotalNum).toBe(4);
    expect(page.textObject).toHaveLength(4);
    expect(page.listObject ?? []).toHaveLength(0);
    expect(page.imageObject ?? []).toHaveLength(0);
    expect(validateEvenHubPageContainer(page as never).valid).toBe(true);
    const byName = Object.fromEntries(page.textObject.map((t) => [t.containerName, t]));
    expect(byName['reader-head']!.content).toBe('NEWS · 1/5 · BBC · 16m · Woman and three children die');
    // The Mac's 62-column rows concatenate losslessly; the firmware wraps.
    expect(byName['main']!.content).toBe('A woman and three children have died in a house fire on Merseyside.\nSix fire engines were at the scene.');
    expect(byName['main']!.isEventCapture).toBe(1);
    expect(page.textObject.filter((t) => t.isEventCapture === 1)).toHaveLength(1);
    expect(byName['hint']!.content).toBe('swipe: more · double-click: back');
    // Every band is whole 27px lines inside its 4px padding and on-canvas.
    for (const t of page.textObject) {
      expect(t.yPosition + t.height).toBeLessThanOrEqual(CANVAS.height);
      expect((t.height - 2 * (t.containerName === 'status' ? 6 : 4)) % 27).toBe(0);
    }
  });

  it('list layout gives full-focus content a title and quiet bottom action rail', () => {
    const page = builder.listLayout(['Messages', 'Settings'], {
      title: 'CAPABILITIES',
      hint: 'SWIPE move · TAP open · LEFT x2 back',   // authored form
    }) as Page;
    const title = page.textObject?.find((c) => c.containerID === ContainerId.Main);
    const list = page.listObject?.[0];

    const hint = page.textObject?.find((c) => c.containerID === ContainerId.Hint);

    expect(page.containerTotalNum).toBe(4);
    expect(title).toMatchObject({ yPosition: 39, height: 38, textColor: 4, content: 'CAPABILITIES' });
    expect(list).toMatchObject({ yPosition: 77, height: 176 });
    expect(hint).toMatchObject({ yPosition: 253, height: 35, textColor: 1 });
    // The wearer sees the FORMATTED hint. Hints are authored in the legacy
    // "left x2" form and GesturePolicy.formatHint rewrites them at render, so
    // a wearer on the R1 ring is never told to press a left temple they do
    // not have. Asserting the authored form here meant this test would have
    // passed while the glasses said "LEFT x2".
    expect(hint?.content).toBe('SWIPE move · TAP open · double-click back');
    expect((hint?.yPosition ?? 0) + (hint?.height ?? 0)).toBe(CANVAS.height);
  });

  it('confirmation is a dedicated full-screen decision surface with no list or button chrome', () => {
    const page = builder.confirmationLayout('Send reply to Adele?') as Page;
    const byId = (id: number) => page.textObject?.find((c) => c.containerID === id);

    expect(page.listObject).toBeUndefined();
    expect(page.containerTotalNum).toBe(5);
    expect(byId(ContainerId.Main)).toMatchObject({
      yPosition: 39, height: 144, textColor: 4,
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
