import { describe, expect, it } from 'vitest';
import { validateEvenHubPageContainer } from '@evenrealities/even_hub_sdk';
import { getTextWidth } from '@evenrealities/pretext';
import { firmwareLineCount } from './FirmwareText';
import { withImageHeader } from './HeaderImage';
import { withHomeImages } from './HomeImages';
import { HudPageBuilder } from './HudPageBuilder';
import {
  NOTIFICATION_CARD, NotificationCardId, cardFromRenderedText, notificationAge, notificationCardBottom,
  notificationCardLayout, overlayNotificationCard,
} from './NotificationCard';
import type { OverviewTextView } from './OverviewText';
import { ContainerId, HomeColumnId, type HudNotificationCard } from './types';

const NOW = 1_791_410_291_592;
const card = (body: string, extra: Partial<HudNotificationCard> = {}): HudNotificationCard => ({
  id: 'a'.repeat(32), app: 'WhatsApp', sender: 'Elizabeth', body, at: NOW, urgent: false, ...extra,
});
const SHORT = 'Mom didn’t pick up the phone. Can you ask her to buy some onions and pepper?';

describe('notification card', () => {
  it('lays out as Even\'s card: app and age, then sender and the whole message, within the page', () => {
    const page = withImageHeader(notificationCardLayout(card(SHORT), { index: 0, count: 3 }, NOW, '9:41'));
    expect(validateEvenHubPageContainer(page)).toEqual({ valid: true });
    const texts = page.textObject ?? [];
    const by = (id: number) => texts.find((t) => t.containerID === id)!;
    expect(by(NotificationCardId.Head).content).toBe('WhatsApp');
    expect(by(NotificationCardId.Meta).content).toBe('Now · 1/3');
    // The sender's name opens the message's first row, not a row of its
    // own, and the wrapped rows stand under the first row's words.
    const lines = by(NotificationCardId.Body).content!.split('\n');
    expect(lines[0]).toMatch(/^Elizabeth: Mom/u);
    expect(lines.length).toBeGreaterThan(1);
    expect(lines.map((line) => line.trim()).join(' ')).toBe(`Elizabeth: ${SHORT}`);
    for (const line of lines.slice(1)) {
      const lead = getTextWidth(/^ */u.exec(line)![0]);
      expect(Math.abs(lead - getTextWidth('Elizabeth: '))).toBeLessThanOrEqual(getTextWidth(' ') / 2);
    }
    // The body takes input; nothing else does.
    expect(texts.filter((t) => t.isEventCapture === 1).map((t) => t.containerID)).toEqual([NotificationCardId.Body]);
    // The body's text fits its lines exactly: no firmware scrollbar.
    const body = by(NotificationCardId.Body);
    expect(firmwareLineCount(body.content!, body.width!)).toBeLessThanOrEqual(body.height! / 27);
    // Under the header, inside the canvas, clear of its sides.
    const frame = by(NotificationCardId.Frame);
    expect(frame.yPosition).toBeGreaterThanOrEqual(24);
    expect((frame.xPosition ?? 0)).toBeGreaterThanOrEqual(16);
    expect((frame.xPosition ?? 0) + (frame.width ?? 0)).toBeLessThanOrEqual(576 - 16);
    expect(notificationCardBottom(page)).toBeLessThanOrEqual(NOTIFICATION_CARD.maxBottom);
  });

  it('grows with the message and fits a very long one with an ellipsis', () => {
    const short = notificationCardBottom(notificationCardLayout(card('Hi'), { index: 0, count: 1 }, NOW, ''))!;
    const long = notificationCardLayout(card('word '.repeat(200)), { index: 0, count: 1 }, NOW, '');
    expect(notificationCardBottom(long)!).toBeGreaterThan(short);
    expect(notificationCardBottom(long)!).toBeLessThanOrEqual(NOTIFICATION_CARD.maxBottom);
    const body = long.textObject!.find((t) => t.containerID === NotificationCardId.Body)!;
    expect(body.content!.endsWith('…')).toBe(true);
  });

  it('says how long ago, as Even does', () => {
    expect(notificationAge(NOW - 5_000, NOW)).toBe('Now');
    expect(notificationAge(NOW - 7 * 60_000, NOW)).toBe('7m ago');
    expect(notificationAge(NOW - 2 * 3_600_000, NOW)).toBe('2h ago');
    expect(notificationAge(NOW - 3 * 86_400_000, NOW)).toBe('3d ago');
  });

  it('reads a card from the Mac\'s short line until the full list arrives', () => {
    expect(cardFromRenderedText('b'.repeat(32), '!! Dele · Messages (2)\nOn my way', NOW)).toEqual({
      id: 'b'.repeat(32), app: 'Messages', sender: 'Dele', body: 'On my way', at: NOW, urgent: true,
    });
  });

  it('floats over Home: the page below the card stays, dimmed; nothing of it under the card', () => {
    const view: OverviewTextView = {
      header: '9:41 · JOAT',
      info: ['Wed, Oct 7', '72f clear', '2 unread'],
      tabs: [
        { label: 'NOTIFICATIONS', active: false }, { label: 'NEWS', active: true },
        { label: 'POMODORO', active: false }, { label: 'SEARCH', active: false },
      ],
      paneLabels: Array.from({ length: 8 }, (_, i) => `${i + 1}h`),
      pane: Array.from({ length: 8 }, (_, i) => `Headline number ${i + 1} about the day`),
      paneBrightness: 4,
      hint: 'tap: open · swipe: tabs',
    };
    const home = withHomeImages(new HudPageBuilder().overviewTextLayout(view));
    const layout = notificationCardLayout(card(SHORT), { index: 0, count: 1 }, NOW, '9:41');
    const page = withImageHeader(overlayNotificationCard(layout, home));
    expect(validateEvenHubPageContainer(page)).toEqual({ valid: true });
    const bottom = notificationCardBottom(page)!;
    const cardIds = new Set<number>(Object.values(NotificationCardId));
    const fromHome = (page.textObject ?? []).filter((t) => !cardIds.has(t.containerID!));
    expect(fromHome.length).toBeGreaterThan(0);
    for (const text of fromHome) {
      expect(text.yPosition!).toBeGreaterThanOrEqual(bottom + NOTIFICATION_CARD.gap);
      expect(text.textColor).toBeLessThanOrEqual(1);
      expect(text.isEventCapture).toBe(0);
    }
    // The pane's lines below the card are its later headlines, not its first.
    const pane = fromHome.find((t) => t.containerID === ContainerId.Main);
    expect(pane?.content).not.toContain('Headline number 1 ');
    expect(pane?.content).toContain('Headline number 8');
    // Home's column stays beneath (the presenter repaints it below the card),
    // and the card is drawn above everything of the page.
    const ids = (page.imageObject ?? []).map((i) => i.containerID);
    expect(ids).toEqual(expect.arrayContaining([HomeColumnId.Top, HomeColumnId.Bottom]));
    const cardZ = Math.min(...(page.textObject ?? []).filter((t) => cardIds.has(t.containerID!)).map((t) => t.zOrderIndex ?? 0));
    for (const item of [...fromHome, ...(page.imageObject ?? []).filter((i) => i.containerID! >= 18)]) {
      expect(item.zOrderIndex ?? 0).toBeLessThan(cardZ);
    }
  });
});
