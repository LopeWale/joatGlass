import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { validateEvenHubPageContainer } from '@evenrealities/even_hub_sdk';
import { getTextWidth, measureTextWrap } from '@evenrealities/pretext';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import type { HudStatusModel } from './HudStatus';
import type { HudVisualFrame, HudVisualSurface } from './HudVisualSurface';
import { PANE_TEXT_WIDTH } from './OverviewText';
import { ContainerId, HomeColumnId } from './types';

/**
 * Home is firmware text (the default): one rebuild in, then only the bands
 * that changed, as flicker-free upgrades, and never an image tile. Each
 * test counts the host calls a wearer's action costs, because latency is
 * the reason this page exists (everything-evenhub glasses-ui: an image
 * frame is 0.5-2 s over BLE, a text upgrade is the fast path).
 */
setNativeOverviewForTest(true);

type Text = { containerID?: number; containerName?: string; content?: string; textColor?: number;
  width?: number; height?: number; paddingLength?: number; borderWidth?: number; isEventCapture?: number };
type Page = RebuildPageContainer & { textObject?: Text[]; imageObject?: unknown[]; listObject?: unknown[] };

function harness() {
  const pages: Page[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const canvas: HudVisualFrame[] = [];
  let epoch = 0;
  const client = {
    pageEpoch: () => epoch,
    require: () => ({
      textContainerUpgrade: async (u: TextContainerUpgrade) => { upgrades.push(u); return true; },
      rebuildPageContainer: async (page: Page) => { epoch += 1; pages.push(page); return true; },
      updateImageRawData: async () => 'success',
    }),
  } as unknown as BridgeClient;
  const visual = {
    paint: async (frame: HudVisualFrame) => { canvas.push(frame); return true; },
    withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    forgetPage: () => undefined,
  } as unknown as HudVisualSurface;
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  const last = (): Page => pages[pages.length - 1]!;
  const named = (page: Page, name: string): Text | undefined =>
    page.textObject?.find((entry) => entry.containerName === name);
  const bumpEpoch = (): void => { epoch += 1; };
  return { hud, pages, upgrades, canvas, last, named, bumpEpoch };
}

const status = (clock: string, extra: Partial<HudStatusModel> = {}): HudStatusModel => ({
  clock, link: 'connected', mic: false, battery: 80, charging: false, ...extra,
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-05T15:00:00Z'));
});
afterEach(() => { vi.useRealTimers(); });

describe('Home as firmware text', () => {
  it('is one rebuild of six text containers: no image container, no tile', async () => {
    const { hud, pages, canvas, last, named } = harness();
    await hud.setStatusModel(status('9:41'));
    expect(pages).toHaveLength(1);
    const page = last();
    expect(page.imageObject ?? []).toHaveLength(0);
    expect(page.textObject?.map((t) => t.containerName)).toEqual(['status', 'info', 'tabs', 'pane-l', 'pane', 'rule', 'hint']);
    expect(page.textObject?.filter((t) => t.isEventCapture === 1).map((t) => t.containerName)).toEqual(['tabs']);
    expect(validateEvenHubPageContainer(page as never).valid).toBe(true);
    expect(canvas).toHaveLength(0);
    // The same live header as every firmware page: clock first, glyphs last.
    expect(named(page, 'status')?.content).toMatch(/^9:41 · .*○ · [▁▂▃▄▅▆▇█]/u);
  });

  it('every band fits its box: no line wraps, no firmware scrollbar', async () => {
    const { hud, last } = harness();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 1,
      headline: 'Updated just now · 12 stories from BBC, Reuters and the AP wire',
      rows: Array.from({ length: 12 }, (_, i) => `[${i}m] A long headline that keeps going well past the pane · BBC`) });
    await hud.stepTab(1);
    await hud.setStatusModel(status('12:59', { macState: 'hearing you and thinking about it at length' }));
    for (const t of last().textObject ?? []) {
      if (t.containerName === 'rule') continue;
      const inset = (t.paddingLength ?? 0) + (t.borderWidth ?? 0);
      const lines = Math.floor(((t.height ?? 0) - 2 * inset) / 27);
      expect(measureTextWrap(t.content ?? '', (t.width ?? 0) - 2 * inset).lineCount, t.containerName)
        .toBeLessThanOrEqual(lines);
    }
  });

  it('an unchanged repaint sends nothing', async () => {
    const { hud, pages, upgrades } = harness();
    await hud.setStatusModel(status('9:41'));
    await hud.setStatusModel(status('9:41'));
    expect(pages).toHaveLength(1);
    expect(upgrades).toHaveLength(0);
  });

  it('a minute tick is one upgrade of the header, never a rebuild', async () => {
    const { hud, pages, upgrades } = harness();
    await hud.setStatusModel(status('9:41'));
    await hud.setStatusModel(status('9:42'));
    expect(pages).toHaveLength(1);
    expect(upgrades).toHaveLength(1);
    expect(upgrades[0]).toMatchObject({ containerID: ContainerId.StatusBar, containerName: 'status' });
    expect(upgrades[0]!.content).toMatch(/^9:42 · /u);
  });

  it('a rail swipe is three upgrades (rail, labels, values), never a rebuild, and moves the > cursor', async () => {
    const { hud, pages, upgrades, canvas } = harness();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 1, rows: ['[1m] Fresh headline · BBC'] });
    await hud.setStatusModel(status('9:41'));
    const rebuilds = pages.length;
    expect(await hud.stepTab(1)).toBe('news');
    expect(pages).toHaveLength(rebuilds);
    expect(upgrades.map((u) => u.containerName).sort()).toEqual(['pane', 'pane-l', 'tabs']);
    const rail = upgrades.find((u) => u.containerName === 'tabs')!.content!.split('\n');
    expect(rail[1]).toBe('> NEWS');
    // Proportional font: every label starts at the same x.
    const indent = (row: string) => getTextWidth(row.slice(0, row.length - row.trimStart().replace(/^> /, '').length));
    expect(new Set(rail.map(indent)).size).toBe(1);
    expect(upgrades.find((u) => u.containerName === 'pane')!.content).toContain('Fresh headline');
    expect(canvas).toHaveLength(0);
  });

  it('shows a Home notification in the pane instead of swallowing it', async () => {
    const { hud, last, named, upgrades } = harness();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 1, rows: ['[1m] A · BBC'] });
    await hud.showText('Marta: lunch at noon?', ContainerId.Main, undefined, 'notification');
    // The grid: the kind in the label column, the words in the value column.
    const labels = upgrades.filter((u) => u.containerName === 'pane-l').pop()?.content ?? named(last(), 'pane-l')?.content;
    const pane = upgrades.filter((u) => u.containerName === 'pane').pop()?.content ?? named(last(), 'pane')?.content;
    expect(labels).toMatch(/^NOTICE/u);
    expect(pane).toContain('Marta: lunch at noon?');
  });

  it('streams a Mac reply into the pane as upgrades', async () => {
    const { hud, pages, upgrades } = harness();
    await hud.showText('Meeting starts in', ContainerId.Main, 4);
    const rebuilds = pages.length;
    vi.advanceTimersByTime(400);
    await hud.appendText(' 19 minutes.', ContainerId.Main);
    expect(pages).toHaveLength(rebuilds);
    expect(upgrades[upgrades.length - 1]).toMatchObject({ containerID: ContainerId.Main });
    expect(upgrades[upgrades.length - 1]!.content).toContain('Meeting starts in 19 minutes.');
  });

  it('draws the Pomodoro with a measured progress bar of firmware glyphs', async () => {
    const { hud, last, named, upgrades } = harness();
    const now = Date.now();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'pomodoro', revision: 1, rows: [],
      pomodoro: { phase: 'focus', running: true, endsAtUnixMs: now + 10 * 60_000, remainingMs: 10 * 60_000,
        lengthMs: 25 * 60_000, completed: 1 } });
    await hud.stepTab(1);
    expect(await hud.stepTab(1)).toBe('pomodoro');
    const pane = (upgrades.filter((u) => u.containerName === 'pane').pop()?.content
      ?? named(last(), 'pane')?.content ?? '').split('\n');
    expect(pane[0]).toBe('Focus · block 2 of 4');
    expect(pane[1]).toBe('10 min left');
    expect(pane[2]).toMatch(/^━+─+$/u);
    expect(getTextWidth(pane[2]!)).toBeLessThanOrEqual(PANE_TEXT_WIDTH);
    expect((pane[2]!.match(/━/gu) ?? []).length / pane[2]!.length).toBeCloseTo(0.6, 1);
    expect(pane[3]).toBe('● ○ ○ ○');
  });

  it('Back from a firmware list is ONE rebuild: the text page replaces the list directly', async () => {
    const { hud, pages, canvas } = harness();
    await hud.setStatusModel(status('9:41'));
    await hud.showList(['[1m] A · BBC', '[2m] B · BBC'], {
      title: 'NEWS', selectable: true, railTab: 'news', nativeFirmwareList: true,
    });
    const before = pages.length;
    expect(await hud.showDefault()).toBe(true);
    expect(pages).toHaveLength(before + 1);
    expect(pages[pages.length - 1]!.textObject?.map((t) => t.containerName)).toContain('pane');
    expect(pages[pages.length - 1]!.imageObject ?? []).toHaveLength(0);
    expect(canvas).toHaveLength(0);
  });

  it('after another page has been drawn, Home rebuilds instead of upgrading a stranger', async () => {
    const { hud, pages, upgrades, bumpEpoch } = harness();
    await hud.setStatusModel(status('9:41'));
    bumpEpoch(); // some other surface rebuilt the page behind the presenter's back
    await hud.setStatusModel(status('9:42'));
    expect(upgrades).toHaveLength(0);
    expect(pages).toHaveLength(2);
  });

  it('a receipt over Home is a firmware page, not an image rebuild', async () => {
    const { hud, pages, canvas } = harness();
    await hud.setStatusModel(status('9:41'));
    expect(await hud.showAssistantOverlay('Saved to Notes', false, true, 'receipt')).toBe(true);
    expect(canvas).toHaveLength(0);
    expect(pages.every((page) => (page.imageObject ?? []).length === 0)).toBe(true);
  });
});


describe('Home with its column as images (HomeImages.ts)', () => {
  function columnHarness() {
    const order: string[] = [];
    const pages: Page[] = [];
    const columns: unknown[] = [];
    const client = {
      pageEpoch: () => pages.length,
      require: () => ({
        textContainerUpgrade: async (u: TextContainerUpgrade) => { order.push(`upgrade:${u.containerName}`); return true; },
        rebuildPageContainer: async (page: Page) => { order.push('rebuild'); pages.push(page); return true; },
        updateImageRawData: async () => 'success',
      }),
    } as unknown as BridgeClient;
    const visual = {
      paint: async () => true,
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
      forgetPage: () => undefined,
      paintHeader: async () => true,
      canPaintHeader: () => true,
      paintHomeColumn: async (view: unknown) => { order.push('column'); columns.push(view); return true; },
      canPaintHomeColumn: () => true,
    } as unknown as HudVisualSurface;
    return { hud: new HudPresenter(client, new HudPageBuilder(), visual), order, pages, columns };
  }

  it('a tab swipe moves the pane in place first, then repaints the column; no rebuild', async () => {
    const { hud, order, pages, columns } = columnHarness();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 1, rows: ['[1m] Fresh headline · BBC'] });
    await hud.setStatusModel(status('9:41'));
    const page = pages[pages.length - 1]!;
    expect(page.imageObject).toHaveLength(4);
    expect(page.textObject?.map((t) => t.containerName)).not.toContain('tabs');
    expect(page.textObject?.filter((t) => t.isEventCapture === 1).map((t) => t.containerName)).toEqual(['pane']);
    const rebuilds = pages.length;
    order.length = 0;

    expect(await hud.stepTab(1)).toBe('news');
    await vi.runAllTimersAsync();
    expect(pages).toHaveLength(rebuilds);
    expect(order.indexOf('upgrade:pane')).toBeGreaterThanOrEqual(0);
    expect(order.indexOf('column')).toBeGreaterThan(order.indexOf('upgrade:pane'));
    const view = columns[columns.length - 1] as { tabs: { tab: string; active: boolean }[] };
    expect(view.tabs.find((tab) => tab.active)?.tab).toBe('news');
  });

  it('opens JOAT\'s box over Home as drawn: the column stays beneath and is repainted above the box', async () => {
    const { hud, pages, columns } = columnHarness();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 1, rows: ['[1m] Fresh headline · BBC'] });
    await hud.setStatusModel(status('9:41'));
    columns.length = 0;
    expect(await hud.showAssistantConversation({
      conversationId: 'c1', revision: 1, state: 'ready', continueAfterMs: 30_000, earlierTurnsOmitted: false,
      turns: [{ role: 'user', text: 'What time is my next meeting?' }, { role: 'assistant', text: 'Design review at 2:30.' }],
    })).toBe(true);
    await vi.runAllTimersAsync();
    const page = pages[pages.length - 1]!;
    const images = (page.imageObject ?? []).map((image) => image.containerID);
    expect(images).toEqual(expect.arrayContaining([HomeColumnId.Top, HomeColumnId.Bottom]));
    const box = page.textObject?.find((text) => text.containerName === 'conversation');
    expect(box?.isEventCapture).toBe(1);
    // Home's pane is still there, above the box.
    expect(page.textObject?.some((text) => text.containerName === 'pane')).toBe(true);
    // The column was repainted with nothing drawn under the box.
    const view = columns[columns.length - 1] as { clipBottom?: number };
    expect(view.clipBottom).toBeDefined();
    expect(view.clipBottom!).toBeLessThanOrEqual(box!.yPosition!);
  });
});
