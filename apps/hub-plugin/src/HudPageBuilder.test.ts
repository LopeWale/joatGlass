import { describe, expect, it } from 'vitest';
import { HudPageBuilder } from './HudPageBuilder';
import { CANVAS, ContainerId } from './types';

type Page = {
  containerTotalNum?: number;
  textObject?: Array<{
    containerID?: number;
    isEventCapture?: number;
    zOrderIndex?: number;
    yPosition?: number;
    height?: number;
    textColor?: number;
    content?: string;
  }>;
  listObject?: Array<{
    containerID?: number;
    isEventCapture?: number;
    zOrderIndex?: number;
    yPosition?: number;
    height?: number;
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
  ['list', builder.listLayout(['a', 'b', 'c']) as Page],
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

  it('list layout keeps status plus compact title/actions around five focus rows', () => {
    const page = builder.listLayout(['Messages', 'Settings'], {
      title: 'CAPABILITIES',
      hint: 'SWIPE move · TAP open · LEFT x2 back',
    }) as Page;
    const title = page.textObject?.find((c) => c.containerID === ContainerId.Main);
    const list = page.listObject?.[0];

    expect(page.containerTotalNum).toBe(3);
    expect(title).toMatchObject({ yPosition: 32, height: 48, textColor: 3 });
    expect(list).toMatchObject({ yPosition: 80, height: 208 });
    expect(title?.content?.endsWith('SWIPE move · TAP open · LEFT x2 back')).toBe(true);
    expect(page.textObject?.some((c) => c.containerID === ContainerId.Hint)).toBe(false);
    expect((list?.yPosition ?? 0) + (list?.height ?? 0)).toBe(CANVAS.height);
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
