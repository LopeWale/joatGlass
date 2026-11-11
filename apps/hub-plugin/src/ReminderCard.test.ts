import { describe, expect, it } from 'vitest';
import { validateEvenHubPageContainer, type RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import { HudPageBuilder, REMINDER_LIST_ROW_TEXT_WIDTH, fitListRow } from './HudPageBuilder';
import {
  REMINDER_CARD,
  REMINDER_LIST,
  hasReminderCard,
  normalizeReminderCard,
  reminderCardLayout,
  reminderCardView,
  withReminderCard,
  type ReminderCard,
} from './ReminderCard';
import { CANVAS, ContainerId, ReminderCardId } from './types';

/** Saturday 24 October 2026, 1:40 PM, LOCAL: the card's words are made from
 * the phone's own clock, so every time below is a local Date too. */
const NOW = new Date(2026, 9, 24, 13, 40).getTime();

const COUNTS = { late: 0, today: 0, tomorrow: 0, later: 0, undated: 0 } as const;

function card(next: Partial<ReminderCard> = {}): ReminderCard {
  return { ...COUNTS, ...next };
}

describe('normalizeReminderCard', () => {
  it('rejects anything that is not a card with five whole, non-negative counts', () => {
    expect(normalizeReminderCard(undefined)).toBeUndefined();
    expect(normalizeReminderCard(null)).toBeUndefined();
    expect(normalizeReminderCard('card')).toBeUndefined();
    const { undated: _missing, ...fourCounts } = COUNTS;
    expect(normalizeReminderCard(fourCounts)).toBeUndefined();
    expect(normalizeReminderCard({ ...COUNTS, late: -1 })).toBeUndefined();
    expect(normalizeReminderCard({ ...COUNTS, today: 1.5 })).toBeUndefined();
    expect(normalizeReminderCard({ ...COUNTS, later: '2' })).toBeUndefined();
    expect(normalizeReminderCard({ ...COUNTS, tomorrow: Number.NaN })).toBeUndefined();
  });

  it('accepts a valid card: one-line title, whole-ms due time, counts capped', () => {
    expect(normalizeReminderCard({
      title: '  Pick up\n the   cake ', dueUnixMs: 1_793_000_000_000.6, allDay: true,
      late: 1, today: 2, tomorrow: 0, later: 3, undated: 5_000,
    })).toEqual({
      title: 'Pick up the cake', dueUnixMs: 1_793_000_000_001, allDay: true,
      late: 1, today: 2, tomorrow: 0, later: 3, undated: 999,
    });
    // allDay is only ever true when the wire says exactly true.
    expect(normalizeReminderCard({ ...COUNTS, title: 'Gym', dueUnixMs: NOW, allDay: 'yes' }))
      .toEqual({ ...COUNTS, title: 'Gym', dueUnixMs: NOW, allDay: false });
  });

  it('drops the next-up reminder unless it has both a title and a finite due time', () => {
    for (const raw of [
      { ...COUNTS, late: 2, title: 'Buy filters' },
      { ...COUNTS, late: 2, title: 'Buy filters', dueUnixMs: Number.POSITIVE_INFINITY },
      { ...COUNTS, late: 2, title: '   ', dueUnixMs: NOW },
      { ...COUNTS, late: 2, dueUnixMs: NOW },
    ]) {
      const normalized = normalizeReminderCard(raw);
      expect(normalized).toEqual({ ...COUNTS, late: 2 });
      expect(normalized).not.toHaveProperty('title');
      expect(normalized).not.toHaveProperty('dueUnixMs');
      expect(normalized).not.toHaveProperty('allDay');
    }
  });
});

describe('reminderCardView', () => {
  it('a timed reminder later today shows its clock time and an hours-and-minutes countdown', () => {
    const view = reminderCardView(card({
      title: 'Pick up the cake', dueUnixMs: new Date(2026, 9, 24, 15, 0).getTime(), today: 1,
    }), NOW);
    expect(view).toMatchObject({
      kicker: 'NEXT', when: '3:00 PM', title: 'Pick up the cake', countdown: 'in 1 h 20 min',
    });
  });

  it('under an hour away counts minutes only', () => {
    const view = reminderCardView(card({
      title: 'Call Mom', dueUnixMs: new Date(2026, 9, 24, 14, 5).getTime(), today: 1,
    }), NOW);
    expect(view.when).toBe('2:05 PM');
    expect(view.countdown).toBe('in 25 min');
  });

  it('a timed reminder tomorrow, more than a day away, says Tomorrow and tomorrow', () => {
    const view = reminderCardView(card({
      title: 'Gym', dueUnixMs: new Date(2026, 9, 25, 15, 0).getTime(), tomorrow: 1,
    }), NOW);
    expect(view.when.startsWith('Tomorrow')).toBe(true);
    expect(view.when).toBe('Tomorrow 3:00 PM');
    expect(view.countdown).toBe('tomorrow');
  });

  it('an all-day reminder two days out names the weekday and counts days', () => {
    const due = new Date(2026, 9, 26).getTime(); // Monday 26 October, local
    const view = reminderCardView(card({ title: 'Renew the car tag', dueUnixMs: due, allDay: true, later: 1 }), NOW);
    expect(view.when).toBe(new Date(due).toLocaleDateString('en-US', { weekday: 'short' }));
    expect(view.when).toBe('Mon');
    expect(view.countdown).toBe('in 2 days');
  });

  it('with no next reminder, says whether only late ones are left', () => {
    const late = reminderCardView(card({ late: 1 }), NOW);
    expect(late.when).toBe('Only late ones');
    expect(late.countdown).toBe('');
    const none = reminderCardView(card(), NOW);
    expect(none.when).toBe('Nothing due');
    expect(none.countdown).toBe('');
  });

  it('names late, today and tomorrow, and folds later and undated into one "more"', () => {
    expect(reminderCardView(card({ late: 1, today: 3, tomorrow: 1, later: 2, undated: 1 }), NOW).counts)
      .toBe('1 late · 3 today · 1 tmrw · 3 more');
    expect(reminderCardView(card({ today: 2, undated: 1 }), NOW).counts).toBe('2 today · 1 more');
    expect(reminderCardView(card(), NOW).counts).toBe('nothing else open');
  });
});

describe('reminderCardLayout', () => {
  const view = reminderCardView(card({
    title: 'Drop the permit forms at the city office before it closes for the weekend',
    dueUnixMs: new Date(2026, 9, 24, 15, 0).getTime(),
    late: 1, today: 3, tomorrow: 1, later: 2, undated: 1,
  }), NOW);

  it('draws every mark inside the card, with its outline', () => {
    const marks = reminderCardLayout(view);
    expect(marks.filter((mark) => mark.frame)).toHaveLength(1);
    for (const mark of marks) {
      expect(mark.x).toBeGreaterThanOrEqual(0);
      expect(mark.y).toBeGreaterThanOrEqual(0);
      expect(mark.x + mark.width).toBeLessThanOrEqual(REMINDER_CARD.width);
      expect(mark.y + mark.height).toBeLessThanOrEqual(REMINDER_CARD.height);
    }
    expect(marks.map((mark) => mark.text)).toContain('1 late · 3 today · 1 tmrw · 3 more');
  });

  it('under JOAT\'s box, drops the outline and every line whose bottom passes the clip', () => {
    const plain = reminderCardLayout(view);
    const clipBottom = REMINDER_CARD.y + 60;
    const clipped = reminderCardLayout({ ...view, clipBottom });
    expect(clipped.some((mark) => mark.frame)).toBe(false);
    for (const mark of clipped) {
      expect(REMINDER_CARD.y + mark.y + mark.height).toBeLessThanOrEqual(clipBottom);
    }
    // Lines are left out whole, never cut: what stays is exactly the plain
    // card's text lines that end above the clip.
    expect(clipped).toEqual(plain.filter((mark) => !mark.frame
      && REMINDER_CARD.y + mark.y + mark.height <= clipBottom));
    expect(clipped.length).toBeGreaterThan(0);
    expect(clipped.length).toBeLessThan(plain.filter((mark) => !mark.frame).length);
  });

  it('under a notification card, keeps only lines below it, dimmed', () => {
    const plain = reminderCardLayout(view);
    const clipTop = REMINDER_CARD.y + 60;
    const under = reminderCardLayout({ ...view, clipTop, dim: true });
    expect(under.length).toBeGreaterThan(0);
    expect(under.some((mark) => mark.frame)).toBe(false);
    for (const mark of under) {
      expect(REMINDER_CARD.y + mark.y).toBeGreaterThanOrEqual(clipTop);
      const same = plain.find((p) => p.text === mark.text && p.y === mark.y)!;
      expect(mark.alpha).toBeLessThan(same.alpha);
    }
  });
});

describe('withReminderCard', () => {
  const ROWS = [
    '[Late] Pay the electric bill',
    '[3:00 PM] Pick up the cake from the bakery on the corner of Main and Fifth',
    '[Tmrw] Gym',
  ];

  function remindersPage(): RebuildPageContainer {
    return new HudPageBuilder().listLayout(ROWS, { title: 'REMINDERS', hint: 'tap: done · 2x tap: back' });
  }

  it('sits the card and the list side by side, each one half of the canvas, under the header', () => {
    expect(REMINDER_CARD).toMatchObject({ y: 35, width: CANVAS.width / 2, height: 144 });
    expect(REMINDER_LIST.width).toBe(CANVAS.width / 2);
    expect([REMINDER_CARD.x, REMINDER_LIST.x].sort((a, b) => a - b)).toEqual([0, CANVAS.width / 2]);
  });

  it('moves the list to its half with rows refitted, and adds one card image', () => {
    const page = remindersPage();
    const before = page.listObject![0]!;
    const fullRows = before.itemContainer!.itemName!;
    const carded = withReminderCard(page);

    const list = carded.listObject!.find((item) => item.containerID === ContainerId.List)!;
    expect(list).toMatchObject({
      xPosition: REMINDER_LIST.x,
      width: REMINDER_LIST.width,
      yPosition: before.yPosition,
      isEventCapture: 1,
    });
    const rows = list.itemContainer!.itemName!;
    expect(rows).toEqual(fullRows.map((row) => fitListRow(row, REMINDER_LIST_ROW_TEXT_WIDTH)));
    expect(list.itemContainer!.itemCount).toBe(rows.length);
    expect(rows[2]).toBe('[Tmrw] Gym');
    expect(rows[1]!.length).toBeLessThan(fullRows[1]!.length);
    expect(rows[1]!.startsWith('[3:00 PM] Pick')).toBe(true);

    expect(carded.imageObject).toHaveLength(1);
    const image = carded.imageObject![0]!;
    expect(image).toMatchObject({
      containerID: ReminderCardId,
      containerName: 'rem-card',
      xPosition: REMINDER_CARD.x,
      yPosition: REMINDER_CARD.y,
      width: REMINDER_CARD.width,
      height: REMINDER_CARD.height,
    });
    expect(ReminderCardId).toBe(21);
    // Drawn above every text and list container on the page.
    for (const other of [...(carded.textObject ?? []), ...(carded.listObject ?? [])]) {
      expect(image.zOrderIndex ?? 0).toBeGreaterThan(other.zOrderIndex ?? 0);
    }

    expect(carded.containerTotalNum).toBe(
      (carded.textObject?.length ?? 0) + (carded.listObject?.length ?? 0) + 1,
    );
    expect(validateEvenHubPageContainer(carded)).toEqual({ valid: true });
    expect(hasReminderCard(carded)).toBe(true);
    expect(hasReminderCard(page)).toBe(false);
    // The input page is left as it was.
    expect(page.imageObject ?? []).toHaveLength(0);
    expect(page.listObject![0]!.xPosition).toBe(0);
    expect(page.listObject![0]!.itemContainer!.itemName).toEqual(fullRows);
  });

  it('returns a page unchanged when it already has images or has no list', () => {
    const carded = withReminderCard(remindersPage());
    expect(withReminderCard(carded)).toBe(carded);
    const text = new HudPageBuilder().assistantAnswerLayout('A reply with no list.');
    expect(text.listObject ?? []).toHaveLength(0);
    expect(withReminderCard(text)).toBe(text);
  });
});
