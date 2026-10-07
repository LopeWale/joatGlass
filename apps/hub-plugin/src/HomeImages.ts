import { ImageContainerProperty, RebuildPageContainer, TextContainerProperty } from '@evenrealities/even_hub_sdk';
import { HEADER_IMAGE_HEIGHT } from './HeaderImage';
import type { OverviewTextView } from './OverviewText';
import { ContainerId, HomeColumnId, PANEL_TABS, type PanelTab } from './types';

/**
 * Home's left column (the date, the weather, the unread count and the tab
 * rail) as two images in Even's font at 66% with icons, the parts of Home
 * that rarely change. The right pane stays firmware text: it changes on
 * every tab swipe, and an in-place text upgrade (~83 ms) is faster than any
 * image send. This is the mixed design the fast G2 plugins use
 * (weather-even-g2, sandevistan): images for what rarely changes, native
 * text for what changes often (the wearer's choice, 2026-10-06).
 *
 * Images are sent only when their pixels change (the image lane's tile
 * cache): a tab swipe resends the rail's tile, the weather or the unread
 * count the other, and the clock lives in the header, so nothing repaints
 * on the minute.
 */

/** The column: under the 24 px header, above Home's hint, left of the rule.
 * Two tiles, each within the SDK's 144 px image height. */
export const HOME_COLUMN = {
  x: 0,
  width: 150,
  top: HEADER_IMAGE_HEIGHT,
  tileHeight: 116,
} as const;

export interface HomeColumnView {
  info: { date: string; weather: string; unread: number };
  tabs: { tab: PanelTab; label: string; active: boolean }[];
}

/** The column's content, from the same view the text column was built from. */
export function homeColumnView(view: OverviewTextView, weather: string, unread: number): HomeColumnView {
  return {
    info: { date: view.info[0] ?? '', weather, unread },
    tabs: view.tabs.map((tab, index) => ({ tab: PANEL_TABS[index] ?? 'notifications', ...tab })),
  };
}

/** Whether Home's page carries the image column. */
export function hasHomeImages(page: RebuildPageContainer): boolean {
  return (page.imageObject ?? []).some((image) => image.containerID === HomeColumnId.Top);
}

/**
 * Home's text page with the info and rail text swapped for the two column
 * images. The pane takes the event capture the rail held (it is fitted to
 * its lines, so it never scrolls and every swipe reaches the plugin). A page
 * without those bands, or without room for two more images, is returned as
 * it is.
 */
export function withHomeImages(page: RebuildPageContainer): RebuildPageContainer {
  const texts = page.textObject ?? [];
  const images = page.imageObject ?? [];
  const dropped = new Set<number>([ContainerId.Context, ContainerId.Tray]);
  if (!texts.some((text) => text.containerID === ContainerId.Tray)
    || !texts.some((text) => text.containerID === ContainerId.Main)
    || images.length > 2 || hasHomeImages(page)) return page;
  const rest = texts
    .filter((text) => !dropped.has(text.containerID ?? -1))
    .map((text) => new TextContainerProperty({
      ...text, isEventCapture: text.containerID === ContainerId.Main ? 1 : 0,
    }));
  const others = [...rest, ...(page.listObject ?? []), ...images];
  const ordered = others.some((item) => item.zOrderIndex !== undefined);
  const top = Math.max(0, ...others.map((item) => item.zOrderIndex ?? 0));
  const tile = (containerID: number, containerName: string, y: number, z: number) => new ImageContainerProperty({
    containerID, containerName, xPosition: HOME_COLUMN.x, yPosition: y,
    width: HOME_COLUMN.width, height: HOME_COLUMN.tileHeight,
    ...(ordered ? { zOrderIndex: z } : {}),
  });
  return new RebuildPageContainer({
    ...page,
    containerTotalNum: rest.length + (page.listObject?.length ?? 0) + images.length + 2,
    textObject: rest,
    imageObject: [
      ...images,
      tile(HomeColumnId.Top, 'home-top', HOME_COLUMN.top, top + 1),
      tile(HomeColumnId.Bottom, 'home-bot', HOME_COLUMN.top + HOME_COLUMN.tileHeight, top + 2),
    ],
  });
}
