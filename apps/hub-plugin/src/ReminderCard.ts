import {
  ImageContainerProperty,
  ListContainerProperty,
  ListItemContainerProperty,
  RebuildPageContainer,
} from '@evenrealities/even_hub_sdk';
import { evenLineHeight, evenTextWidth, fitOneLineEven, wrapEven } from './EvenFont';
import { FIRMWARE_LINE_HEIGHT } from './FirmwareText';
import { REMINDER_LIST_ROW_TEXT_WIDTH, fitListRow } from './HudPageBuilder';
import { CANVAS, ContainerId, ReminderCardId } from './types';

/**
 * The Reminders page (the wearer's pick, 2026-10-24): the next reminder as an
 * image card on the left, the part that is looked at; every open reminder as
 * Even's own list on the right, the part that is tapped (a tap asks to mark
 * it done). Images for what changes rarely, firmware for what is used most,
 * as on Home (HomeImages.ts). The calendar month it replaces is gone.
 *
 * The brain sends the facts (`hud.list.reminderCard`); the words are made
 * here at paint time, so "in 1 h 20 min" stays true: the card is repainted
 * with the header's clock, and the image lane sends it only when its pixels
 * changed.
 */

/** What the brain sends with the Reminders list. */
export interface ReminderCard {
  /** The next reminder that is not late; absent when there is none. */
  title?: string;
  dueUnixMs?: number;
  /** A day with no time. */
  allDay?: boolean;
  late: number;
  today: number;
  tomorrow: number;
  later: number;
  undated: number;
}

/** The card's image: left half, under the header line, beside the list.
 * `y` is HudPageBuilder's NATIVE_HEADER_HEIGHT (one firmware line inside
 * 4 px of padding), spelled out here: importing that constant at load time
 * would close an import cycle (HudPageBuilder → HudCanvasRenderer → here). */
export const REMINDER_CARD = {
  x: 0,
  y: FIRMWARE_LINE_HEIGHT + 2 * 4,
  width: CANVAS.width / 2,
  height: 144,
} as const;

/** The list beside it: the right half. */
export const REMINDER_LIST = { x: CANVAS.width / 2, width: CANVAS.width / 2 } as const;

const MAX_COUNT = 999;

/** The wire's card, or undefined when it is not one. */
export function normalizeReminderCard(raw: unknown): ReminderCard | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined;
  const r = raw as Record<string, unknown>;
  const count = (value: unknown) => (
    Number.isInteger(value) && (value as number) >= 0 ? Math.min(MAX_COUNT, value as number) : undefined
  );
  const late = count(r.late);
  const today = count(r.today);
  const tomorrow = count(r.tomorrow);
  const later = count(r.later);
  const undated = count(r.undated);
  if (late === undefined || today === undefined || tomorrow === undefined
    || later === undefined || undated === undefined) return undefined;
  const card: ReminderCard = { late, today, tomorrow, later, undated };
  if (typeof r.title === 'string' && r.title.trim() && typeof r.dueUnixMs === 'number'
    && Number.isFinite(r.dueUnixMs)) {
    card.title = r.title.replace(/\s+/gu, ' ').trim().slice(0, 160);
    card.dueUnixMs = Math.round(r.dueUnixMs);
    card.allDay = r.allDay === true;
  }
  return card;
}

/** The card's words at `now`, and how it sits under a box or a card. */
export interface ReminderCardView {
  kicker: string;
  when: string;
  title: string;
  countdown: string;
  counts: string;
  /** Under JOAT's box: nothing at or below this page y. */
  clipBottom?: number;
  /** Under a notification card: nothing above this page y. */
  clipTop?: number;
  dim?: boolean;
}

const DAY_MS = 86_400_000;

function dayStart(ms: number): number {
  const d = new Date(ms);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** Whole calendar days from `now`'s day to `ms`'s day (DST-safe). */
function daysApart(ms: number, now: number): number {
  return Math.round((dayStart(ms) - dayStart(now)) / DAY_MS);
}

function clock(ms: number): string {
  return new Date(ms).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

function dayName(ms: number, days: number): string {
  const d = new Date(ms);
  if (days === 0) return 'Today';
  if (days === 1) return 'Tomorrow';
  if (days > 1 && days < 7) return d.toLocaleDateString('en-US', { weekday: 'short' });
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/** "in 25 min", "in 1 h 20 min", "in 3 days", "due now". */
function countdown(due: number, now: number, allDay: boolean): string {
  const days = daysApart(due, now);
  if (allDay) return days <= 0 ? 'today' : days === 1 ? 'tomorrow' : `in ${days} days`;
  const minutes = Math.floor((due - now) / 60_000);
  if (minutes < 1) return 'due now';
  if (minutes < 60) return `in ${minutes} min`;
  if (minutes < 24 * 60) {
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return m === 0 ? `in ${h} h` : `in ${h} h ${m} min`;
  }
  return days === 1 ? 'tomorrow' : `in ${days} days`;
}

export function reminderCardView(card: ReminderCard, now: number): ReminderCardView {
  // Late, today and tomorrow by name; the rest folded into one count, so
  // the line fits the card ("1 no d…" in the simulator otherwise).
  const more = card.later + card.undated;
  const parts = [
    card.late > 0 ? `${card.late} late` : '',
    card.today > 0 ? `${card.today} today` : '',
    card.tomorrow > 0 ? `${card.tomorrow} tmrw` : '',
    more > 0 ? `${more} more` : '',
  ].filter(Boolean);
  const counts = parts.length > 0 ? parts.join(' · ') : 'nothing else open';
  if (card.title === undefined || card.dueUnixMs === undefined) {
    return {
      kicker: 'NEXT',
      when: card.late > 0 ? 'Only late ones' : 'Nothing due',
      title: card.late > 0 ? 'Tap one on the right to mark it done.' : 'Say: remind me to … at …',
      countdown: '',
      counts,
    };
  }
  const due = card.dueUnixMs;
  const days = daysApart(due, now);
  const allDay = card.allDay === true;
  const when = allDay ? dayName(due, days)
    : days === 0 ? clock(due)
      : `${dayName(due, days)} ${clock(due)}`;
  return { kicker: 'NEXT', when, title: card.title, countdown: countdown(due, now, allDay), counts };
}

/** One thing the card draws, in card coordinates. Pure, so the margins are
 * tested without a canvas. */
export interface ReminderCardMark {
  x: number;
  y: number;
  width: number;
  height: number;
  alpha: number;
  text?: string;
  /** Even-font scale (0.66 by default). */
  scale?: number;
  /** The card's outline. */
  frame?: boolean;
}

const PAD = 16;
const BIG = 1;
const UNDER_CARD_ALPHA = 0.45;

export function reminderCardLayout(view: ReminderCardView): ReminderCardMark[] {
  const inner = REMINDER_CARD.width - 2 * PAD;
  const line = evenLineHeight();
  const bigLine = evenLineHeight(BIG);
  const marks: ReminderCardMark[] = [
    { x: 4, y: 2, width: REMINDER_CARD.width - 8, height: REMINDER_CARD.height - 4, alpha: 0.4, frame: true },
  ];
  const words = (text: string, y: number, alpha: number, scale?: number) => {
    const fitted = fitOneLineEven(text, inner, scale);
    if (!fitted) return;
    marks.push({
      x: PAD, y, width: evenTextWidth(fitted, scale), height: scale === undefined ? line : evenLineHeight(scale),
      alpha, text: fitted, ...(scale === undefined ? {} : { scale }),
    });
  };
  let y = 8;
  words(view.kicker, y, 0.6);
  y += line;
  words(view.when, y, 1, BIG);
  y += bigLine + 2;
  const titleLines = wrapEven(view.title, inner);
  const shown = titleLines.slice(0, 2);
  if (titleLines.length > 2) shown[1] = fitOneLineEven(`${shown[1]} ${titleLines.slice(2).join(' ')}`, inner);
  for (const text of shown) { words(text, y, 0.9); y += line; }
  if (shown.length < 2) y += line;
  if (view.countdown) words(view.countdown, y, 0.75);
  y += line;
  words(view.counts, y, 0.55);
  if (view.dim) for (const mark of marks) mark.alpha *= UNDER_CARD_ALPHA;
  const { clipBottom, clipTop } = view;
  if (clipBottom === undefined && clipTop === undefined) return marks;
  // Under a box or a card a line is left out whole, never cut through a
  // glyph; the outline goes with it.
  return marks.filter((mark) => {
    if (mark.frame) return false;
    const top = REMINDER_CARD.y + mark.y;
    return (clipBottom === undefined || top + mark.height <= clipBottom)
      && (clipTop === undefined || top >= clipTop);
  });
}

/** Whether a page carries the card. */
export function hasReminderCard(page: RebuildPageContainer): boolean {
  return (page.imageObject ?? []).some((image) => image.containerID === ReminderCardId);
}

/**
 * A list page with its list moved to the right half (rows refitted to the
 * narrower width) and the card's image on the left. A page without the
 * list, or already carrying images, is returned as it is.
 */
export function withReminderCard(page: RebuildPageContainer): RebuildPageContainer {
  const list = (page.listObject ?? []).find((item) => item.containerID === ContainerId.List);
  const images = page.imageObject ?? [];
  if (!list || images.length > 0) return page;
  const rows = (list.itemContainer?.itemName ?? []).map((row) => fitListRow(row, REMINDER_LIST_ROW_TEXT_WIDTH));
  const narrowed = new ListContainerProperty({
    ...list,
    xPosition: REMINDER_LIST.x,
    width: REMINDER_LIST.width,
    // The half's own row width (the full-width list fills 576 the same way).
    itemContainer: new ListItemContainerProperty({
      ...list.itemContainer, itemCount: rows.length, itemName: rows,
      itemWidth: REMINDER_LIST.width - 2 * (list.paddingLength ?? 0),
    }),
  });
  const others = [...(page.textObject ?? []), narrowed];
  const ordered = others.some((item) => item.zOrderIndex !== undefined);
  const top = Math.max(0, ...others.map((item) => item.zOrderIndex ?? 0));
  return new RebuildPageContainer({
    ...page,
    containerTotalNum: (page.textObject?.length ?? 0) + (page.listObject?.length ?? 0) + 1,
    listObject: (page.listObject ?? []).map((item) => (item === list ? narrowed : item)),
    imageObject: [new ImageContainerProperty({
      containerID: ReminderCardId,
      containerName: 'rem-card',
      xPosition: REMINDER_CARD.x,
      yPosition: REMINDER_CARD.y,
      width: REMINDER_CARD.width,
      height: REMINDER_CARD.height,
      ...(ordered ? { zOrderIndex: top + 1 } : {}),
    })],
  });
}
