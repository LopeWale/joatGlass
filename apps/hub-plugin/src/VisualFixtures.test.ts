import { describe, expect, it, vi } from 'vitest';

import {
  ASSISTANT_OVERLAY_VISUAL_TEXT,
  VISUAL_CONFIRMATION_PROMPT,
  terminalVisualFixture,
  isTerminalVisualState,
  runVisualConfirmationFixture,
  isProductivityVisualState,
  stitchHudPreviewRgba,
  visualListFixture,
} from './VisualFixtures';

describe('visual fixtures', () => {
  it('uses realistic content-free copy for the assistant overlay lens check', () => {
    expect(ASSISTANT_OVERLAY_VISUAL_TEXT).toContain('calendar and reminders');
    expect(ASSISTANT_OVERLAY_VISUAL_TEXT).toContain('monitoring is armed');
  });

  it('stitches renderer tiles into one native-size green optical preview', () => {
    const pixels = (value: number) => new Uint8Array(288 * 144).fill(value);
    const rgba = stitchHudPreviewRgba([
      { containerId: 4, pixels: pixels(17) },
      { containerId: 8, pixels: pixels(34) },
      { containerId: 9, pixels: pixels(51) },
      { containerId: 10, pixels: pixels(68) },
    ]);
    const greenAt = (x: number, y: number) => rgba[(y * 576 + x) * 4 + 1];
    expect(rgba).toHaveLength(576 * 288 * 4);
    expect(greenAt(0, 0)).toBe(17);
    expect(greenAt(575, 0)).toBe(34);
    expect(greenAt(0, 287)).toBe(51);
    expect(greenAt(575, 287)).toBe(68);
    // One strict pass over all 165,888 pixels: a per-pixel expect() costs
    // more than the stitch itself and timed the test out. The first byte
    // that is not pure opaque green is reported by offset.
    let firstOffChannel = -1;
    for (let offset = 0; offset < rgba.length; offset += 4) {
      if (rgba[offset] !== 0 || rgba[offset + 2] !== 0 || rgba[offset + 3] !== 255) {
        firstOffChannel = offset;
        break;
      }
    }
    expect(firstOffChannel).toBe(-1);
  });

  it('covers every productivity collection and durable write receipt', () => {
    expect(visualListFixture('notifications').title).toBe('NOTIFICATIONS');
    expect(visualListFixture('reminders').title).toBe('REMINDERS · 8 OPEN');
    expect(visualListFixture('tasks').title).toBe('TASKS');
    expect(visualListFixture('quicknotes').title).toBe('QUICKNOTES');
    expect(visualListFixture('activity').title).toBe('ACTIVITY');
    expect(visualListFixture('action')).toEqual({
      title: 'ACTION · DONE',
      hint: 'left x2 overview',
      items: ['Calendar create · Saved and verified'],
      selectable: false,
    });
    expect(visualListFixture('messages').selectable).toBe(true);
    expect(visualListFixture('notification-detail').selectable).toBe(false);
    expect(visualListFixture('calendar-detail')).toMatchObject({ title: 'CALENDAR · DETAIL', hint: 'left x2 Reminders' });
    expect(visualListFixture('task-detail')).toMatchObject({ title: 'TASKS · DETAIL', hint: 'left x2 Reminders' });
    expect(visualListFixture('quicknote-detail').title).toBe('QUICKNOTES · NOTE');
    // A recording's note page is one document, its sections in order.
    const note = visualListFixture('quicknote-detail').items.join('\n');
    const order = ['\n\nOVERVIEW\n', '\n\nKEY POINTS\n', '\n\nNEXT STEPS\n', '\n\nTRANSCRIPT\n'];
    expect(order.map((section) => note.indexOf(section))).toEqual(
      order.map((section) => note.indexOf(section)).sort((a, b) => a - b),
    );
    expect(order.every((section) => note.includes(section))).toBe(true);
    // The live recorder owns the QuickNotes page with tap-only controls.
    expect(visualListFixture('quicknote-recording')).toMatchObject({
      title: 'QUICKNOTES · RECORDING',
      selectable: true,
      items: ['Save and finish', 'Pause recording', 'Discard recording'],
    });
    expect(visualListFixture('quicknote-discard').items).toEqual(
      ['Confirm discard', 'Keep recording'],
    );
    expect(visualListFixture('quicknote-saving').selectable).toBe(false);
    expect(visualListFixture('activity-detail').title).toBe('ACTIVITY · DETAIL');
    expect(isProductivityVisualState('notification-detail')).toBe(true);
    expect(isProductivityVisualState('overview')).toBe(false);
  });

  it('covers Agent Terminal visual states and 3-level picker lists', () => {
    expect(terminalVisualFixture('agent-terminal-ready')).toMatchObject({
      state: 'ready', focus: 'Claude · idle',
    });
    expect(terminalVisualFixture('agent-terminal-working')).toMatchObject({
      state: 'running', focus: 'Claude · working',
    });
    expect(terminalVisualFixture('agent-terminal-approval')).toMatchObject({
      state: 'running', approvals: 1,
    });
    expect(terminalVisualFixture('agent-terminal-blocked')).toMatchObject({
      state: 'blocked', focus: 'Claude · blocked',
    });
    expect(visualListFixture('terminal-sessions')).toMatchObject({
      title: 'AGENT TERMINAL',
      selectable: true,
    });
    expect(isTerminalVisualState('agent-terminal-ready')).toBe(true);
    expect(isTerminalVisualState('agent-terminal-approval')).toBe(true);
    expect(isTerminalVisualState('overview')).toBe(false);
  });

  it('makes the confirmation fixture unmistakably test-only', () => {
    expect(VISUAL_CONFIRMATION_PROMPT).toContain('SIMULATOR TEST (NO SEND)');
    expect(VISUAL_CONFIRMATION_PROMPT).toContain('No message will be sent');
    expect(VISUAL_CONFIRMATION_PROMPT).not.toContain('mockups');
  });

  it('arms the fixture gate and dismisses it with the simulator verdict', async () => {
    let settle: ((accepted: boolean) => void) | undefined;
    const hud = {
      showConfirmation: vi.fn(async () => true),
      dismissConfirmation: vi.fn(async () => true),
    };
    const imu = {
      prepare: vi.fn((_actionId: string, onResult: (accepted: boolean) => void) => {
        settle = onResult;
      }),
      activate: vi.fn(() => true),
      cancel: vi.fn(() => true),
    };

    await expect(runVisualConfirmationFixture(hud, imu)).resolves.toBe(true);
    expect(imu.prepare).toHaveBeenCalledWith('visual-confirmation', expect.any(Function));
    expect(imu.activate).toHaveBeenCalledWith('visual-confirmation', 8000);

    settle?.(true);
    expect(hud.dismissConfirmation).toHaveBeenCalledWith('visual-confirmation');
  });
});

describe('panel visual fixtures', () => {
  it('shape every row the way the Mac producers do, within the wire caps', async () => {
    const { PANEL_VISUAL_STATES, isPanelVisualState, panelArticleFixture, panelVisualPushes, panelVisualTab } = await import('./VisualFixtures');
    const pushes = panelVisualPushes(1_000_000);
    expect(pushes.map((push) => push.tab)).toEqual(['planner', 'news', 'pomodoro', 'search']);
    for (const push of pushes) {
      expect(push.rows.length).toBeLessThanOrEqual(20);
      for (const row of push.rows) expect(row.length).toBeLessThanOrEqual(64);
    }
    // The Planner's rows are what `planner_rows` writes: "[Late] …",
    // "[09:00] …", "[Today] …", "[Tmrw] …", "[Fri] …", and a closing "+N more".
    const planner = pushes[0]!;
    expect(planner.rows).toHaveLength(8); // the pane's eight lines, all used
    for (const row of planner.rows) expect(row).toMatch(/^(\[(Late|Today|Tmrw|\d\d:\d\d|[A-Z][a-z]{2})\] .+|\+\d+ more)$/);
    const news = pushes[1]!;
    expect(news.rows.length).toBeGreaterThan(10); // the expanded page must scroll
    for (const row of news.rows) expect(row).toMatch(/^\[\d+[mh]\] .+ · .+$/);
    const pomodoro = pushes[2]!.pomodoro!;
    expect(pomodoro.endsAtUnixMs).toBe(1_000_000 + pomodoro.remainingMs);
    expect(pomodoro.remainingMs).toBeLessThanOrEqual(pomodoro.lengthMs);
    // The article arrives whole: no list rows, the text in `body`, and a
    // footer that promises no Mac page (the plugin moves through it).
    const article = panelArticleFixture();
    expect(article.items).toEqual([]);
    expect(article.body.split('\n').length).toBeGreaterThan(5);
    // The Mac's article template: headline, source and age, a blank line,
    // then paragraphs a single blank line apart.
    expect(article.body.split('\n').slice(0, 3)).toEqual(['Consumer sentiment rises to 48.9', 'Fixture Wire · 12m', '']);
    expect(article.body).not.toMatch(/\n\n\n/u);
    expect(article.hint).not.toMatch(/down more|up previous/);
    expect(PANEL_VISUAL_STATES.every(isPanelVisualState)).toBe(true);
    expect(isPanelVisualState('overview')).toBe(false);
    expect(panelVisualTab('panel-search-expanded')).toBe('search');
    expect(panelVisualTab('panel-planner')).toBe('planner');
    expect(panelVisualTab('panel-article')).toBe('news');
  });

  it('keeps panel fixtures free of phone numbers and email addresses', async () => {
    const { panelArticleFixture, panelVisualPushes } = await import('./VisualFixtures');
    const text = JSON.stringify([panelVisualPushes(), panelArticleFixture()]);
    expect(text).not.toMatch(/\+1\s*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}/);
    expect(text).not.toMatch(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
  });
});
