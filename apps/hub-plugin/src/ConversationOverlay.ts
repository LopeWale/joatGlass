import {
  ImageContainerProperty,
  ListContainerProperty,
  RebuildPageContainer,
  TextContainerProperty,
} from '@evenrealities/even_hub_sdk';
import { containerLineCapacity, fitLines, firmwareLineCount } from './FirmwareText';
import { ContainerId } from './types';

/**
 * JOAT's conversation box floats over the page it was called on, as Even
 * AI's card floats over whatever the wearer was looking at (the wearer's
 * ask, 2026-10-06): the page keeps everything above the box, and the box
 * grows upward over it.
 *
 * The G2 has no fill colour: black pixels are off, so a native box cannot
 * hide what lies beneath it. The page therefore gives up the area under the
 * box. Each of its containers is cut to end above the box's top border, or
 * dropped if not one line of it fits, and its text is fitted to the rows
 * left so nothing overflows (an overflowing container draws the firmware's
 * own scrollbar). The box keeps the page's one event capture, and the
 * page's status line, which the box carries itself.
 */

/** Space between the page above and the box's top border. */
export const OVERLAY_GAP = 6;
/** The SDK's page limits: 8 text and list containers, 4 images, 12 in all. */
const MAX_TEXT_AND_LIST = 8;
const MAX_IMAGES = 4;
const MAX_CONTAINERS = 12;
const LIST_ROW_HEIGHT = 40;

type Placed = TextContainerProperty | ListContainerProperty;

/** The y of the box's top border: the page above it ends `OVERLAY_GAP` higher. */
export function cardTop(card: RebuildPageContainer): number | undefined {
  return card.textObject?.find((text) => text.containerName === 'conversation')?.yPosition;
}

function cutText(text: TextContainerProperty, limit: number): TextContainerProperty | undefined {
  const y = text.yPosition ?? 0;
  const height = text.height ?? 0;
  const pad = text.paddingLength ?? 0;
  const border = text.borderWidth ?? 0;
  const room = Math.min(height, limit - y);
  if (room <= 0) return undefined;
  const content = text.content ?? ' ';
  // A container that only draws its border (the overview's column rule)
  // carries no text; it is cut like any box.
  const drawsText = content.trim().length > 0;
  const lines = containerLineCapacity(room, pad, border);
  if (drawsText && lines < 1) return undefined;
  if (!drawsText && room < 2 * border + 2) return undefined;
  const innerWidth = (text.width ?? 0) - 2 * (pad + border);
  const fitted = drawsText && firmwareLineCount(content, innerWidth) > lines
    ? fitLines(content, innerWidth, lines)
    : content;
  return new TextContainerProperty({ ...text, height: room, content: fitted, isEventCapture: 0 });
}

function cutList(list: ListContainerProperty, limit: number): ListContainerProperty | undefined {
  const y = list.yPosition ?? 0;
  const room = Math.min(list.height ?? 0, limit - y);
  if (room < LIST_ROW_HEIGHT + 2 * (list.paddingLength ?? 0)) return undefined;
  return new ListContainerProperty({ ...list, height: room, isEventCapture: 0 });
}

/**
 * The box (`card`, the conversation layout) over the page (`underlay`, that
 * page's own layout as it would be rebuilt now). The page's text is cut to
 * end above the box; its images (Home's column) stay beneath it, and the
 * presenter repaints them with nothing drawn under the box. Without an
 * underlay the box stands alone.
 */
export function overlayConversation(
  card: RebuildPageContainer,
  underlay: RebuildPageContainer | undefined,
): RebuildPageContainer {
  const top = cardTop(card);
  if (!underlay || top === undefined) return card;
  const underImages = underlay.imageObject ?? [];
  // A page drawn wholly in pixels from the top (a reader in Even's font):
  // its tiles stay and the presenter repaints them clipped.
  if (underImages.some((image) => (image.yPosition ?? 0) === 0)) return overImagePage(card, underlay);
  // Any other images (Home's column) stay beneath the box as they are; the
  // presenter repaints them with nothing drawn under the box. The box's own
  // status line stays: it becomes the page's image header, which leaves room
  // for at most two of the page's images.
  if ((card.imageObject?.length ?? 0) > 0 || underImages.length > MAX_IMAGES - 2) return card;
  const limit = top - OVERLAY_GAP;
  const cardTexts = card.textObject ?? [];
  const room = Math.min(
    MAX_TEXT_AND_LIST - cardTexts.length - (card.listObject?.length ?? 0),
    MAX_CONTAINERS - cardTexts.length - (card.listObject?.length ?? 0) - underImages.length - 2,
  );
  if (room <= 0) return card;
  const kept: Placed[] = [];
  for (const text of underlay.textObject ?? []) {
    if (text.containerID === ContainerId.StatusBar) continue;
    // An empty, borderless container draws nothing (an input layer).
    if (!(text.content ?? '').trim() && !(text.borderWidth ?? 0)) continue;
    const cut = cutText(text, limit);
    if (cut) kept.push(cut);
  }
  for (const list of underlay.listObject ?? []) {
    const cut = cutList(list, limit);
    if (cut) kept.push(cut);
  }
  if (kept.length === 0 && underImages.length === 0) return card;
  // Highest first: when the page has more than the limits leave room for,
  // the top of the page is what the wearer still sees above the box.
  kept.sort((a, b) => (a.yPosition ?? 0) - (b.yPosition ?? 0));
  const under = kept.slice(0, room);
  // zOrderIndex is all or nothing per page: the page's images lowest, its
  // text above them in its own order, and every container of the box on top.
  const images = underImages.map((image, index) => new ImageContainerProperty({ ...image, zOrderIndex: index + 1 }));
  under.forEach((item, index) => { item.zOrderIndex = images.length + index + 1; });
  const lift = images.length + under.length;
  const raise = <T extends { zOrderIndex?: number }>(item: T): T => (
    { ...item, zOrderIndex: (item.zOrderIndex ?? 0) + lift }
  );
  const texts = [
    ...under.filter((item): item is TextContainerProperty => item instanceof TextContainerProperty),
    ...cardTexts.map((text) => new TextContainerProperty(raise(text))),
  ];
  const lists = [
    ...under.filter((item): item is ListContainerProperty => item instanceof ListContainerProperty),
    ...(card.listObject ?? []).map((list) => new ListContainerProperty(raise(list))),
  ];
  return new RebuildPageContainer({
    ...card,
    containerTotalNum: texts.length + lists.length + images.length,
    textObject: texts,
    ...(lists.length > 0 ? { listObject: lists } : {}),
    ...(images.length > 0 ? { imageObject: images } : {}),
  });
}

/**
 * The box over an image page the presenter repaints for it: a reader set in
 * Even's font (EvenFont.ts). The page's image tiles stay beneath the box,
 * and the presenter repaints them with nothing drawn below the box's top
 * border, so the reader stays in its own small font above the box instead
 * of falling back to the firmware's. The page's input layer and the box's
 * own status line go: the box takes input, and the tiles draw the header.
 */
function overImagePage(card: RebuildPageContainer, underlay: RebuildPageContainer): RebuildPageContainer {
  const images = underlay.imageObject ?? [];
  const cardTexts = (card.textObject ?? []).filter((text) => text.containerID !== ContainerId.StatusBar);
  const cardLists = card.listObject ?? [];
  if ((card.imageObject?.length ?? 0) > 0 || images.length > MAX_IMAGES
    || images.length + cardTexts.length + cardLists.length > MAX_CONTAINERS) return card;
  // zOrderIndex is all or nothing per page: the tiles beneath, the box above.
  const tiles = images.map((image, index) => new ImageContainerProperty({ ...image, zOrderIndex: index + 1 }));
  const lift = tiles.length;
  const raise = <T extends { zOrderIndex?: number }>(item: T): T => (
    { ...item, zOrderIndex: (item.zOrderIndex ?? 0) + lift }
  );
  return new RebuildPageContainer({
    ...card,
    containerTotalNum: cardTexts.length + cardLists.length + tiles.length,
    textObject: cardTexts.map((text) => new TextContainerProperty(raise(text))),
    ...(cardLists.length > 0 ? { listObject: cardLists.map((list) => new ListContainerProperty(raise(list))) } : {}),
    imageObject: tiles,
  });
}
