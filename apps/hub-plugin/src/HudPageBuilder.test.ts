import { describe, expect, it } from 'vitest';
import { validateEvenHubPageContainer } from '@evenrealities/even_hub_sdk';
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

  it('builds the conversation as one bordered, bottom-anchored native surface', () => {
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
    expect(byName('conversation')).toMatchObject({
      yPosition: 128,
      height: 132,
      borderWidth: 1,
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

  it('list layout gives full-focus content a title and quiet bottom action rail', () => {
    const page = builder.listLayout(['Messages', 'Settings'], {
      title: 'CAPABILITIES',
      hint: 'SWIPE move · TAP open · LEFT x2 back',
    }) as Page;
    const title = page.textObject?.find((c) => c.containerID === ContainerId.Main);
    const list = page.listObject?.[0];

    const hint = page.textObject?.find((c) => c.containerID === ContainerId.Hint);

    expect(page.containerTotalNum).toBe(4);
    expect(title).toMatchObject({ yPosition: 39, height: 38, textColor: 4, content: 'CAPABILITIES' });
    expect(list).toMatchObject({ yPosition: 77, height: 176 });
    expect(hint).toMatchObject({ yPosition: 253, height: 35, textColor: 1 });
    expect(hint?.content).toBe('SWIPE move · TAP open · LEFT x2 back');
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
