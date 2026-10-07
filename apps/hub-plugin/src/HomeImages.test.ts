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
      expect(mark.y + Math.max(mark.height, mark.icon ? 0 : evenLineHeight())).toBeLessThanOrEqual(HOME_COLUMN.tileHeight);
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
});
