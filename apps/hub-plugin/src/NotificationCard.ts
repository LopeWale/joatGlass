import {
  ImageContainerProperty,
  ListContainerProperty,
  ListItemContainerProperty,
  RebuildPageContainer,
  TextContainerProperty,
} from '@evenrealities/even_hub_sdk';
import { getTextWidth } from '@evenrealities/pretext';
import { FIRMWARE_LINE_HEIGHT, fitLines, firmwareLineCount, paginateMeasured } from './FirmwareText';
import { TEXT_CONTAINER_MAX_BYTES, fitUtf8 } from './FirmwareLimits';
import { CANVAS, ContainerId, type HudNotificationCard } from './types';

/**
 * JOAT's notification card, laid out the way Even's own G2 notification is
 * (Even's support article "Notifications", its glasses image measured 1:1 on
 * the 576x288 canvas, 2026-10-07): a bordered card near the top over the
 * page the wearer was on, the app on its first line with the age at the
 * right, then the sender and the whole message. The page stays beneath it,
 * dimmed, below the card.
 *
 * Firmware text, not pixels: a tap moves to the next unread card, and a text
 * page swaps in one page write, where an image card would cost about 104 ms
 * per tile over Bluetooth for every frame (Even's own slide between cards is
 * the firmware's animation, which the SDK does not offer a plugin).
 */

/** Page-local ids, clear of the page's own (1-10, 16-20) and JOAT's box (11-15). */
export const NotificationCardId = { Frame: 21, Head: 22, Meta: 23, Body: 24 } as const;

export const NOTIFICATION_CARD = {
  left: 40,
  width: 496,
  /** Under the 24 px image header, as Even's card sits under its status. */
  top: 30,
  /** The text inside the frame: 16 px in from its sides, 8 px from its top. */
  inset: 16,
  topInset: 8,
  /** The card never runs past here, so a line of the page shows below it. */
  maxBottom: 252,
  /** Between the card and the dimmed page below it. */
  gap: 6,
} as const;

/** How long a card that popped up on its own stays before closing; the
 * wearer's taps restart it. Even's app shows 3 s; a card here holds the
 * whole message, so it stays a little longer. */
export const NOTIFICATION_CARD_MS = 5000;

const BODY_WIDTH = NOTIFICATION_CARD.width - 2 * NOTIFICATION_CARD.inset;

/** Even's age words: "Now", "7m ago", "2h ago", "3d ago". */
export function notificationAge(atMs: number, nowMs: number): string {
  const seconds = Math.max(0, (nowMs - atMs) / 1000);
  if (seconds < 60) return 'Now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

/** A card from a Mac-rendered notification line ("Who · App\nsummary") for
 * the moment before the full list arrives. */
export function cardFromRenderedText(id: string, text: string, nowMs: number): HudNotificationCard {
  const [head = '', ...rest] = text.split('\n');
  const parts = head.replace(/^!!\s*/u, '').split(' · ');
  return {
    id,
    app: parts.length > 1 ? parts[parts.length - 1]!.replace(/\s*\(\d+\)$/u, '') : '',
    sender: parts[0] ?? '',
    body: rest.join(' ').trim(),
    at: nowMs,
    urgent: /^!!/u.test(head),
  };
}

/** The card's text lines: the sender, then the message, fitted to the room. */
function bodyText(card: HudNotificationCard, maxLines: number): string {
  const sender = card.sender.trim();
  const message = card.body.trim() || ' ';
  const text = sender ? `${sender}\n${message}` : message;
  return fitLines(text, BODY_WIDTH, maxLines);
}

export interface NotificationCardGeometry {
  frameTop: number;
  frameHeight: number;
  bodyTop: number;
  bodyLines: number;
  bottom: number;
}

export function notificationCardGeometry(card: HudNotificationCard): NotificationCardGeometry {
  const c = NOTIFICATION_CARD;
  const bodyTop = c.top + c.topInset + FIRMWARE_LINE_HEIGHT;
  const roomLines = Math.floor((c.maxBottom - c.topInset - bodyTop) / FIRMWARE_LINE_HEIGHT);
  const wanted = firmwareLineCount(bodyText(card, 99), BODY_WIDTH);
  const bodyLines = Math.max(1, Math.min(roomLines, wanted));
  const frameHeight = c.topInset + FIRMWARE_LINE_HEIGHT + bodyLines * FIRMWARE_LINE_HEIGHT + c.topInset;
  return { frameTop: c.top, frameHeight, bodyTop, bodyLines, bottom: c.top + frameHeight };
}

/**
 * The card alone: the page's status line (the image header), the frame,
 * the app line with the age and the card's place among the unread at its
 * right, and the sender with the message. The body takes input; the
 * message is fitted to the card with an ellipsis only past ~7 lines.
 */
export function notificationCardLayout(
  card: HudNotificationCard,
  position: { index: number; count: number },
  nowMs: number,
  status: string,
): RebuildPageContainer {
  const c = NOTIFICATION_CARD;
  const g = notificationCardGeometry(card);
  const meta = [notificationAge(card.at, nowMs), position.count > 1 ? `${position.index + 1}/${position.count}` : '']
    .filter(Boolean).join(' · ');
  const metaWidth = Math.min(BODY_WIDTH, getTextWidth(meta) + 4);
  const text = (id: number, name: string, x: number, y: number, width: number, height: number,
    content: string, textColor: number, z: number, extra: Partial<TextContainerProperty> = {}) =>
    new TextContainerProperty({
      containerID: id, containerName: name, xPosition: x, yPosition: y, width, height,
      paddingLength: 0, borderWidth: 0, isEventCapture: 0, zOrderIndex: z, textColor,
      content: fitUtf8(content || ' ', TEXT_CONTAINER_MAX_BYTES), ...extra,
    });
  const appLabel = card.app.trim() || 'Notification';
  return new RebuildPageContainer({
    containerTotalNum: 5,
    textObject: [
      text(ContainerId.StatusBar, 'status', 0, 0, CANVAS.width, 35, status, 2, 1),
      text(NotificationCardId.Frame, 'note-frame', c.left, g.frameTop, c.width, g.frameHeight, ' ', 0, 2, {
        borderWidth: 1, borderColor: 12, borderRadius: 4,
      }),
      text(NotificationCardId.Head, 'note-head', c.left + c.inset, g.frameTop + c.topInset,
        BODY_WIDTH - metaWidth - 8, FIRMWARE_LINE_HEIGHT,
        fitLines(appLabel, BODY_WIDTH - metaWidth - 8, 1), 2, 3),
      text(NotificationCardId.Meta, 'note-meta', c.left + c.inset + BODY_WIDTH - metaWidth, g.frameTop + c.topInset,
        metaWidth, FIRMWARE_LINE_HEIGHT, meta, 2, 4),
      text(NotificationCardId.Body, 'note-body', c.left + c.inset, g.bodyTop, BODY_WIDTH,
        g.bodyLines * FIRMWARE_LINE_HEIGHT, bodyText(card, g.bodyLines), 4, 5,
        { isEventCapture: 1 }),
    ],
  });
}

/** The y below which the page shows beneath a card. */
export function notificationCardBottom(page: RebuildPageContainer): number | undefined {
  const frame = page.textObject?.find((t) => t.containerName === 'note-frame');
  return frame ? (frame.yPosition ?? 0) + (frame.height ?? 0) : undefined;
}

const MAX_TEXT_AND_LIST = 8;
const MAX_IMAGES = 4;
const LIST_ROW_HEIGHT = 40;
/** The dimmest firmware text, for the page beneath the card (Even dims it). */
const DIM = 1;

/** A text container with the lines that fall under the card left out: it
 * starts below the card, at the line it reached there. Undefined when
 * nothing of it shows below the card. */
function cutTextBelow(text: TextContainerProperty, limit: number): TextContainerProperty | undefined {
  const y = text.yPosition ?? 0;
  const height = text.height ?? 0;
  const pad = text.paddingLength ?? 0;
  const border = text.borderWidth ?? 0;
  if (y >= limit) return new TextContainerProperty({ ...text, isEventCapture: 0, textColor: Math.min(text.textColor ?? DIM, DIM) });
  const content = text.content ?? '';
  const drawsText = content.trim().length > 0;
  if (!drawsText) return undefined;
  const hiddenLines = Math.ceil((limit - (y + pad + border)) / FIRMWARE_LINE_HEIGHT);
  const newY = y + hiddenLines * FIRMWARE_LINE_HEIGHT;
  const newHeight = height - hiddenLines * FIRMWARE_LINE_HEIGHT;
  if (hiddenLines <= 0 || newHeight < FIRMWARE_LINE_HEIGHT + 2 * (pad + border)) return undefined;
  const innerWidth = (text.width ?? 0) - 2 * (pad + border);
  const pages = paginateMeasured(content, innerWidth, hiddenLines);
  const rest = pages.slice(1).join('\n');
  if (!rest.trim()) return undefined;
  const lines = Math.floor((newHeight - 2 * (pad + border)) / FIRMWARE_LINE_HEIGHT);
  return new TextContainerProperty({
    ...text,
    yPosition: newY,
    height: newHeight,
    content: fitLines(rest, innerWidth, lines),
    isEventCapture: 0,
    textColor: Math.min(text.textColor ?? DIM, DIM),
  });
}

/** A firmware list with the rows under the card left out. */
function cutListBelow(list: ListContainerProperty, limit: number): ListContainerProperty | undefined {
  const y = list.yPosition ?? 0;
  const pad = list.paddingLength ?? 0;
  const names = list.itemContainer?.itemName ?? [];
  const hidden = y >= limit ? 0 : Math.ceil((limit - (y + pad)) / LIST_ROW_HEIGHT);
  const rest = names.slice(hidden);
  const height = (list.height ?? 0) - hidden * LIST_ROW_HEIGHT;
  if (rest.length === 0 || height < LIST_ROW_HEIGHT + 2 * pad) return undefined;
  return new ListContainerProperty({
    ...list,
    yPosition: y + hidden * LIST_ROW_HEIGHT,
    height,
    isEventCapture: 0,
    // No selection border: under the card the list takes no input.
    itemContainer: new ListItemContainerProperty({
      ...list.itemContainer, itemCount: rest.length, itemName: rest, isItemSelectBorderEn: 0,
    }),
  });
}

/**
 * The card over the page it popped up on (`underlay`: the page's own layout,
 * as the presenter rebuilds it for JOAT's box). Everything of the page under
 * the card is left out and the rest is dimmed; the page's images (Home's
 * column) stay and are repainted by the presenter with nothing drawn under
 * the card. Without an underlay the card stands alone.
 */
export function overlayNotificationCard(
  card: RebuildPageContainer,
  underlay: RebuildPageContainer | undefined,
): RebuildPageContainer {
  const bottom = notificationCardBottom(card);
  if (!underlay || bottom === undefined) return card;
  const images = underlay.imageObject ?? [];
  // A page drawn wholly in pixels from the top (a reader in Even's font)
  // cannot be cut; the card stands alone over it.
  if (images.some((image) => (image.yPosition ?? 0) === 0) || images.length > MAX_IMAGES - 2) return card;
  const limit = bottom + NOTIFICATION_CARD.gap;
  const cardTexts = card.textObject ?? [];
  // The card's status line becomes the image header: it does not count.
  const room = MAX_TEXT_AND_LIST - (cardTexts.length - 1);
  const kept: Array<TextContainerProperty | ListContainerProperty> = [];
  for (const text of underlay.textObject ?? []) {
    if (text.containerID === ContainerId.StatusBar) continue;
    if (!(text.content ?? '').trim() && !(text.borderWidth ?? 0)) continue;
    const cut = cutTextBelow(text, limit);
    if (cut) kept.push(cut);
  }
  for (const list of underlay.listObject ?? []) {
    const cut = cutListBelow(list, limit);
    if (cut) kept.push(cut);
  }
  kept.sort((a, b) => (a.yPosition ?? 0) - (b.yPosition ?? 0));
  const under = kept.slice(0, Math.max(0, room));
  const tiles = images.map((image, index) => new ImageContainerProperty({ ...image, zOrderIndex: index + 1 }));
  under.forEach((item, index) => { item.zOrderIndex = tiles.length + index + 1; });
  const lift = tiles.length + under.length;
  const raised = cardTexts.map((text) => new TextContainerProperty({ ...text, zOrderIndex: (text.zOrderIndex ?? 0) + lift }));
  const texts = [
    ...under.filter((item): item is TextContainerProperty => item instanceof TextContainerProperty),
    ...raised,
  ];
  const lists = under.filter((item): item is ListContainerProperty => item instanceof ListContainerProperty);
  return new RebuildPageContainer({
    ...card,
    containerTotalNum: texts.length + lists.length + tiles.length,
    textObject: texts,
    ...(lists.length > 0 ? { listObject: lists } : {}),
    ...(tiles.length > 0 ? { imageObject: tiles } : {}),
  });
}
