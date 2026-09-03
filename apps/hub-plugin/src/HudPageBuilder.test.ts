import { describe, expect, it } from 'vitest';
import { validateEvenHubPageContainer } from '@evenrealities/even_hub_sdk';
import { HudPageBuilder } from './HudPageBuilder';
import { CANVAS, CAPABILITIES, CAPABILITY_MENU_ID_BASE, ContainerId, SPINE_LABEL_BOX, SPINE_LABEL_NAME } from './types';

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
];

describe('HudPageBuilder invariants', () => {
  // Schema regression only. The packaged phone has already shown that SDK
  // validation and simulator acceptance do not prove native-host acceptance.
  it.each(LAYOUTS)('%s: passes the installed SDK page validator', (_name, page) => {
    expect(validateEvenHubPageContainer(page)).toEqual({ valid: true });
  });

  it('makes the one-shot startup an image-first hardware surface', () => {
    const startup = builder.visualStartup() as Page;
    expect(startup.containerTotalNum).toBe(6);
    expect(startup.menuObject?.menuItems).toContainEqual({
      itemID: CAPABILITY_MENU_ID_BASE + CAPABILITIES.indexOf('Planning'),
      itemName: 'Planning',
    });
    expect(startup.imageObject).toHaveLength(4);
    expect(startup.textObject).toHaveLength(2);
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

  it('layers the native spine word above every tile without touching input', () => {
    // The state word is text, not pixels: a voice turn's "hearing you…"
    // costs a textContainerUpgrade, never a 0.5-2 s image tile. It must
    // render in FRONT of the tiles and must never steal the capture.
    for (const page of [builder.visualStartup() as Page, builder.visualLayout() as Page]) {
      const label = page.textObject?.find((text) => text.containerID === ContainerId.SpineLabel);
      expect(label).toMatchObject({
        containerName: SPINE_LABEL_NAME,
        xPosition: SPINE_LABEL_BOX.x,
        yPosition: SPINE_LABEL_BOX.y,
        width: SPINE_LABEL_BOX.width,
        height: SPINE_LABEL_BOX.height,
        isEventCapture: 0,
        content: ' ',
      });
      const highestTile = Math.max(...(page.imageObject ?? []).map((image) => image.zOrderIndex ?? 0));
      expect(label?.zOrderIndex ?? 0).toBeGreaterThan(highestTile);
    }
    // One firmware line tall, inside the top-left tile, east of the date.
    expect(SPINE_LABEL_BOX.height).toBe(27);
    expect(SPINE_LABEL_BOX.x).toBeGreaterThanOrEqual(30 + 9 * 7.2);
    expect(SPINE_LABEL_BOX.x + SPINE_LABEL_BOX.width).toBeLessThanOrEqual(288);
    expect(SPINE_LABEL_BOX.y + SPINE_LABEL_BOX.height).toBeLessThanOrEqual(144);
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
    expect(title).toMatchObject({ yPosition: 32, height: 40, textColor: 4, content: 'CAPABILITIES' });
    expect(list).toMatchObject({ yPosition: 72, height: 184 });
    expect(hint).toMatchObject({ yPosition: 256, height: 32, textColor: 1 });
    expect(hint?.content).toBe('SWIPE move · TAP open · LEFT x2 back');
    expect((hint?.yPosition ?? 0) + (hint?.height ?? 0)).toBe(CANVAS.height);
  });

  it('confirmation is a dedicated full-screen decision surface with no list or button chrome', () => {
    const page = builder.confirmationLayout('Send reply to Adele?') as Page;
    const byId = (id: number) => page.textObject?.find((c) => c.containerID === id);

    expect(page.listObject).toBeUndefined();
    expect(page.containerTotalNum).toBe(5);
    expect(byId(ContainerId.Main)).toMatchObject({
      yPosition: 32, height: 144, textColor: 4,
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

    expect(page.containerTotalNum).toBe(6);
    expect(page.textObject).toEqual([
      expect.objectContaining({
        xPosition: 0, yPosition: 0, width: 576, height: 288,
        content: ' ', textColor: 0, isEventCapture: 1, zOrderIndex: 1,
      }),
      // The native state word rides above the tiles and never captures.
      expect.objectContaining({
        xPosition: SPINE_LABEL_BOX.x, yPosition: SPINE_LABEL_BOX.y,
        width: SPINE_LABEL_BOX.width, height: SPINE_LABEL_BOX.height,
        content: ' ', isEventCapture: 0, zOrderIndex: 6,
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
