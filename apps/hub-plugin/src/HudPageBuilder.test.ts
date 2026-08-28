import { describe, expect, it } from 'vitest';
import { HudPageBuilder } from './HudPageBuilder';
import { CANVAS, ContainerId } from './types';

type Page = {
  containerTotalNum?: number;
  textObject?: Array<{
    containerID?: number;
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
  imageObject?: Array<{ containerID?: number; isEventCapture?: number; zOrderIndex?: number }>;
};

function containers(page: Page) {
  return [...(page.textObject ?? []), ...(page.listObject ?? []), ...(page.imageObject ?? [])];
}

const builder = new HudPageBuilder();
const LAYOUTS: ReadonlyArray<[string, Page]> = [
  ['startup', builder.startup() as Page],
  ['default', builder.defaultLayout() as Page],
  ['sidebar', builder.sidebarLayout(['a', 'b', 'c'], {}) as Page],
  ['list', builder.listLayout(['a', 'b', 'c']) as Page],
  ['confirmation', builder.confirmationLayout('Send reply?') as Page],
  ['visual', builder.visualLayout() as Page],
];

describe('HudPageBuilder invariants', () => {
  it.each(LAYOUTS)('%s: exactly one container captures events', (_name, page) => {
    const capturing = containers(page).filter((c) => c.isEventCapture === 1);
    expect(capturing).toHaveLength(1);
  });

  it.each(LAYOUTS)('%s: zOrderIndex is set on every container and unique', (_name, page) => {
    const all = containers(page);
    const z = all.map((c) => c.zOrderIndex);
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

  it('sidebar layout confines navigation to the left and preserves a dim overview on the right', () => {
    const page = builder.sidebarLayout(
      ['Messages', 'Notifications', 'Calendar', 'Debate', 'Settings'],
      {
        main: 'Next\nDesign review · 4:30',
        context: 'Last query  nearest coffee shop?',
        tray: '2 notifications',
      },
    ) as Page;
    const list = page.listObject?.[0];
    const main = page.textObject?.find((c) => c.containerID === ContainerId.Main);
    const context = page.textObject?.find((c) => c.containerID === ContainerId.Context);

    expect(page.containerTotalNum).toBe(6);
    expect(list).toMatchObject({
      xPosition: 0, yPosition: 32, width: 160, height: 256,
      borderWidth: 1, borderColor: 15, isEventCapture: 1,
    });
    expect(list?.itemContainer).toMatchObject({ itemCount: 5 });
    expect(main).toMatchObject({
      xPosition: 164, yPosition: 32, width: 412, height: 128,
      textColor: 2, content: 'Next\nDesign review · 4:30', isEventCapture: 0,
    });
    expect(context).toMatchObject({ xPosition: 164, width: 412, textColor: 1 });
    expect(containers(page).filter((c) => c.isEventCapture === 1)).toHaveLength(1);
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
    expect(byId(ContainerId.Context)?.content).toBe('RIGHT / RING / NOD  confirm');
    expect(byId(ContainerId.Tray)?.content).toBe('LEFT  cancel');
    expect(byId(ContainerId.Hint)?.content).toBe('8 seconds · timeout cancels');
  });

  it('visual layout tiles the full canvas behind one transparent gesture layer', () => {
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
        content: '', textColor: 0, isEventCapture: 1, zOrderIndex: 5,
      }),
    ]);
    expect(page.imageObject).toEqual([
      expect.objectContaining({ xPosition: 0, yPosition: 0, width: 288, height: 144, zOrderIndex: 1 }),
      expect.objectContaining({ xPosition: 288, yPosition: 0, width: 288, height: 144, zOrderIndex: 2 }),
      expect.objectContaining({ xPosition: 0, yPosition: 144, width: 288, height: 144, zOrderIndex: 3 }),
      expect.objectContaining({ xPosition: 288, yPosition: 144, width: 288, height: 144, zOrderIndex: 4 }),
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
