import { describe, expect, it } from 'vitest';
import { validateEvenHubPageContainer, type RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import { withImageHeader } from './HeaderImage';
import { HudPageBuilder } from './HudPageBuilder';
import { HOME_COLUMN, hasHomeImages, homeColumnView, withHomeImages } from './HomeImages';
import { homeColumnLayout } from './HudCanvasRenderer';
import { evenLineHeight } from './EvenFont';
import { ContainerId, HomeColumnId } from './types';
import type { OverviewTextView } from './OverviewText';

const VIEW: OverviewTextView = {
  header: '9:41 · JOAT',
  info: ['Tue, Oct 6', '72f clear', '2 unread'],
  tabs: [
    { label: 'NOTIFICATIONS', active: true },
    { label: 'NEWS', active: false },
    { label: 'POMODORO', active: false },
    { label: 'SEARCH', active: false },
  ],
  paneLabels: ['', ''],
  pane: ['No new notifications', 'tap: open notifications'],
  paneBrightness: 4,
  hint: 'tap: open · swipe: tabs',
};

function home(): RebuildPageContainer {
  return withHomeImages(withImageHeader(new HudPageBuilder().overviewTextLayout(VIEW)));
}

describe('Home column images', () => {
  it('swaps the info and rail text for two images, within the SDK\'s limits', () => {
    const page = home();
    expect(hasHomeImages(page)).toBe(true);
    const texts = page.textObject ?? [];
    expect(texts.some((text) => text.containerID === ContainerId.Context)).toBe(false);
    expect(texts.some((text) => text.containerID === ContainerId.Tray)).toBe(false);
    expect(page.imageObject).toHaveLength(4);
    expect(texts.length + (page.listObject?.length ?? 0)).toBeLessThanOrEqual(8);
    expect(page.containerTotalNum).toBe(texts.length + 4);
    expect(validateEvenHubPageContainer(page)).toEqual({ valid: true });
    // The pane takes input: exactly one capture, and it is the pane.
    const capture = texts.filter((text) => text.isEventCapture === 1);
    expect(capture.map((text) => text.containerID)).toEqual([ContainerId.Main]);
    // The pane itself is untouched.
    const before = new HudPageBuilder().overviewTextLayout(VIEW).textObject?.find((t) => t.containerID === ContainerId.Main);
    const after = texts.find((text) => text.containerID === ContainerId.Main);
    expect(after?.content).toBe(before?.content);
    expect([after?.xPosition, after?.yPosition, after?.width, after?.height])
      .toEqual([before?.xPosition, before?.yPosition, before?.width, before?.height]);
    // The column sits under the header and above the hint.
    const tiles = page.imageObject!.filter((image) => image.containerID === HomeColumnId.Top
      || image.containerID === HomeColumnId.Bottom);
    const hint = texts.find((text) => text.containerID === ContainerId.Hint);
    for (const tile of tiles) {
      expect(tile.height).toBeLessThanOrEqual(144);
      expect((tile.yPosition ?? 0) + (tile.height ?? 0)).toBeLessThanOrEqual(hint?.yPosition ?? 288);
    }
  });

  it('leaves a page that has no room or no rail as it is', () => {
    const page = home();
    expect(withHomeImages(page)).toBe(page);
    const list = new HudPageBuilder().listLayout(['a', 'b'], { title: 'NEWS' });
    expect(withHomeImages(list)).toBe(list);
  });

  it('draws every mark inside the column, clear of the edge, the rule and the tile seam', () => {
    const view = homeColumnView({
      ...VIEW,
      tabs: VIEW.tabs.map((tab, index) => ({ ...tab, label: index === 0 ? 'A VERY LONG TAB NAME INDEED' : tab.label })),
    }, '104f thunderstorms with heavy rain', 1234);
    for (const mark of homeColumnLayout(view)) {
      expect(mark.x).toBeGreaterThanOrEqual(16);
      expect(mark.x + mark.width).toBeLessThanOrEqual(HOME_COLUMN.width - 16);
      expect(mark.y).toBeGreaterThanOrEqual(0);
      expect(mark.y + Math.max(mark.height, mark.icon || mark.bar ? 0 : evenLineHeight())).toBeLessThanOrEqual(HOME_COLUMN.tileHeight);
    }
  });

  it('puts the date, weather and unread in the top tile and the rail in the bottom one', () => {
    const marks = homeColumnLayout(homeColumnView(VIEW, '72f clear', 2));
    expect(marks.filter((mark) => mark.tile === 0 && mark.text).map((mark) => mark.text))
      .toEqual(['Tue, Oct 6', '72f clear', '2 unread']);
    const rail = marks.filter((mark) => mark.tile === 1 && mark.text);
    expect(rail.map((mark) => mark.text)).toEqual(['NOTIFICATIONS', 'NEWS', 'POMODORO', 'SEARCH']);
    expect(rail[0]!.alpha).toBeGreaterThan(rail[1]!.alpha);
    expect(marks.filter((mark) => mark.tile === 1 && mark.icon).map((mark) => mark.icon))
      .toEqual(['notifications', 'news', 'pomodoro', 'search']);
  });

  it('underlines the lit tab, and the underline moves with a swipe', () => {
    const underline = (active: number) => {
      const view = homeColumnView(VIEW, '72f clear', 2);
      const marks = homeColumnLayout({ ...view, tabs: view.tabs.map((tab, index) => ({ ...tab, active: index === active })) });
      const bars = marks.filter((mark) => mark.bar);
      expect(bars).toHaveLength(1);
      const bar = bars[0]!;
      // Under its own label, as wide as the label, below the glyphs.
      const label = marks.find((mark) => mark.tile === 1 && mark.text && mark.y < bar.y && bar.y - mark.y < 26 && mark.x === bar.x);
      expect(label?.width).toBe(bar.width);
      expect(bar.y).toBeGreaterThanOrEqual(label!.y + evenLineHeight());
      return { y: bar.y, label: label!.text };
    };
    expect(underline(0).label).toBe('NOTIFICATIONS');
    expect(underline(1).label).toBe('NEWS');
    expect(underline(3).label).toBe('SEARCH');
    expect(underline(1).y).toBeGreaterThan(underline(0).y);
  });

  it('under JOAT\'s box, leaves out whole rows at or below the box', () => {
    const clipBottom = 200;
    const marks = homeColumnLayout({ ...homeColumnView(VIEW, '72f clear', 2), clipBottom });
    const line = evenLineHeight();
    for (const mark of marks) {
      const pageTop = HOME_COLUMN.top + (mark.tile === 1 ? HOME_COLUMN.tileHeight : 0) + mark.y;
      expect(pageTop + Math.max(mark.height, line)).toBeLessThanOrEqual(clipBottom);
    }
    const rail = marks.filter((mark) => mark.tile === 1 && mark.text).map((mark) => mark.text);
    // The first rows of the rail sit above the box; the last ones are gone, not cut.
    expect(rail.length).toBeGreaterThan(0);
    expect(rail.length).toBeLessThan(4);
    expect(rail[0]).toBe('NOTIFICATIONS');
  });

  it('under a notification card, shows only the rows below it, dimmed', () => {
    const plain = homeColumnLayout(homeColumnView(VIEW, '72f clear', 2));
    const under = homeColumnLayout({ ...homeColumnView(VIEW, '72f clear', 2), clipTop: 180, dim: true });
    expect(under.length).toBeGreaterThan(0);
    expect(under.length).toBeLessThan(plain.length);
    for (const mark of under) {
      expect(HOME_COLUMN.top + (mark.tile === 1 ? HOME_COLUMN.tileHeight : 0) + mark.y).toBeGreaterThanOrEqual(180);
      const same = plain.find((p) => p.text === mark.text && p.icon === mark.icon && p.y === mark.y && p.tile === mark.tile)!;
      expect(mark.alpha).toBeLessThan(same.alpha);
    }
  });
});
