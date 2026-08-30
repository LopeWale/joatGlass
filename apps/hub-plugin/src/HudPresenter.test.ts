import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ImageRawDataUpdateResult,
  type TextContainerUpgrade,
} from '@evenrealities/even_hub_sdk';
import { HudPresenter } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import {
  HudVisualSurface,
  type HudFrameRenderer,
  type HudVisualFrame,
} from './HudVisualSurface';
import { CANVAS, ContainerId } from './types';
import { navigationVisualFixture } from './NavigationHud';

/**
 * Streamed paints must be SELF-CONTAINED full frames, throttled to BLE
 * reality. Per-token offset splicing rendered as flashing fragments on real
 * hardware ("cutting out, one line") — these tests pin the replacement
 * semantics so it cannot come back.
 */

const paints: TextContainerUpgrade[] = [];

let paintOk = true;
let rebuildOk = true;
let rebuilds = 0;
const rebuiltPages: unknown[] = [];

function fakeClient(): BridgeClient {
  return {
    require: () => ({
      textContainerUpgrade: async (u: TextContainerUpgrade) => {
        paints.push(u);
        return paintOk;
      },
      rebuildPageContainer: async (page: unknown) => {
        rebuilds += 1;
        rebuiltPages.push(page);
        paintOk = rebuildOk; // a successful rebuild restores the containers
        return rebuildOk;
      },
    }),
  } as unknown as BridgeClient;
}

function mainPaints(): TextContainerUpgrade[] {
  return paints.filter((p) => p.containerID === ContainerId.Main);
}

function lastOf(list: TextContainerUpgrade[]): TextContainerUpgrade {
  return list[list.length - 1]!;
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  paints.length = 0;
  paintOk = true;
  rebuildOk = true;
  rebuilds = 0;
  rebuiltPages.length = 0;
  vi.useRealTimers();
});

describe('HudPresenter streaming', () => {
  it('every paint is a full frame from offset 0 — never a splice', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.appendText('Hello ');
    vi.advanceTimersByTime(400);
    await hud.appendText('world');
    vi.advanceTimersByTime(400);
    for (const p of mainPaints()) expect(p.contentOffset).toBe(0);
    const last = lastOf(mainPaints());
    expect(last.content).toBe('Hello world');
  });

  it('a token burst coalesces into few paints, and the tail always lands', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    const tokens = 'The quick brown fox jumps over the lazy dog'.split(/(?<= )/);
    for (const t of tokens) {
      await hud.appendText(t);
      vi.advanceTimersByTime(10); // ~100 tokens/sec, far faster than BLE
    }
    vi.advanceTimersByTime(400); // trailing paint fires
    const n = mainPaints().length;
    expect(n).toBeLessThanOrEqual(4); // not one per token (9 tokens)
    expect(lastOf(mainPaints()).content).toBe('The quick brown fox jumps over the lazy dog');
  });

  it('showText resets the stream — the next append builds on the shown text', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.appendText('stale stream');
    await hud.showText('fresh');
    vi.advanceTimersByTime(400);
    await hud.appendText(' tail');
    vi.advanceTimersByTime(400);
    expect(lastOf(mainPaints()).content).toBe('fresh tail');
  });

  it('contentLength covers the previous frame so stale text cannot survive', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showText('a long line of text');
    vi.advanceTimersByTime(400);
    await hud.showText('short');
    const last = lastOf(mainPaints());
    expect(last.contentLength).toBe('a long line of text'.length);
  });

  it('overflow keeps the tail — the newest words are the ones being read', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.appendText('x'.repeat(1990));
    vi.advanceTimersByTime(400);
    await hud.appendText('THE-END-' + 'y'.repeat(100));
    vi.advanceTimersByTime(400);
    const last = lastOf(mainPaints());
    expect(last.content!.length).toBe(2000);
    expect(last.content!.endsWith('y'.repeat(100))).toBe(true);
  });

  it('status bar and main stream independently', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.appendText('reply text');
    await hud.setStatus('JOAT · listening');
    vi.advanceTimersByTime(400);
    expect(lastOf(mainPaints()).content).toBe('reply text');
    const status = paints.filter((p) => p.containerID === ContainerId.StatusBar);
    expect(lastOf(status).content).toBe('JOAT · listening');
  });
});

describe('HudPresenter brightness (textColor)', () => {
  /**
   * The Mac computes brightness per card (urgent bright, routine dim) and
   * the old handler silently dropped the field — the feature had never
   * worked on glass (confirmed by both audit passes).
   */
  it('showText with a level carries textColor on the upgrade', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showText('!! urgent card', ContainerId.Main, 4);
    expect(lastOf(mainPaints()).textColor).toBe(4);
  });

  it('showText without a level omits the field — SDK keeps current brightness', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showText('plain', ContainerId.Main);
    expect(lastOf(mainPaints()).textColor).toBeUndefined();
  });

  it('appends inherit the container brightness set by the last show', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showText('start ', ContainerId.Main, 3);
    vi.advanceTimersByTime(400);
    await hud.appendText('more');
    vi.advanceTimersByTime(400);
    expect(lastOf(mainPaints()).textColor).toBe(3);
    expect(lastOf(mainPaints()).content).toBe('start more');
  });
});

describe('status-bar single ownership', () => {
  it('setStatus routes to the registered owner instead of painting container 1', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    const announced: string[] = [];
    hud.onStatus = (t) => announced.push(t);
    await hud.setStatus('JOAT · settings');
    expect(announced).toEqual(['JOAT · settings']);
    expect(paints.filter((p) => p.containerID === ContainerId.StatusBar)).toEqual([]);
  });

  it('falls back to a direct paint before an owner registers (early boot is never mute)', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.setStatus('JOAT · connecting');
    const bar = paints.filter((p) => p.containerID === ContainerId.StatusBar);
    expect(bar.length).toBe(1);
  });
});

describe('HudPresenter modal surfaces', () => {
  it('serializes an old blocked visual paint before the newer final frame', async () => {
    const rendered: string[] = [];
    let releaseOld!: () => void;
    const oldUploadBlocked = new Promise<void>((resolve) => { releaseOld = resolve; });
    let firstUpload = true;
    const client = {
      require: () => ({
        updateImageRawData: async () => {
          if (firstUpload) {
            firstUpload = false;
            await oldUploadBlocked;
          }
          return ImageRawDataUpdateResult.success;
        },
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async (frame) => {
        rendered.push(frame.kind === 'overview' ? frame.main : frame.kind);
        return [{ containerId: ContainerId.Image, pixels: new Uint8Array(4) }];
      },
    };
    const hud = new HudPresenter(
      client,
      new HudPageBuilder(),
      new HudVisualSurface(client, renderer),
    );

    const oldPaint = hud.showText('old generation', ContainerId.Main);
    await Promise.resolve();
    const newPaint = hud.showText('new generation', ContainerId.Main);
    await Promise.resolve();
    expect(rendered).toEqual(['old generation']);

    releaseOld();
    await Promise.all([oldPaint, newPaint]);
    expect(rendered).toEqual(['old generation', 'new generation']);
  });

  it('renders navigation as a dedicated focus surface and restores deferred overview content', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    await hud.showText('9:41   JOAT ready', ContainerId.StatusBar);
    await hud.showText('home before navigation', ContainerId.Main);
    await hud.showNavigation(navigationVisualFixture('nav-guidance'));
    await hud.showText('home updated behind navigation', ContainerId.Main);

    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'navigation',
      status: '9:41   JOAT ready',
      navigation: { view: 'guidance', primary: 'Turn right' },
    });

    await hud.showDefault();
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview', main: 'home updated behind navigation',
    });
  });

  it('composites a JOAT overlay over navigation, then removes it without replacing navigation', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    const destination = navigationVisualFixture('nav-guidance');

    await hud.showNavigation(destination);
    await hud.showAssistantOverlay('Turn right in 200 metres.');

    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'navigation',
      navigation: destination,
      assistantOverlay: { text: 'Turn right in 200 metres.' },
    });

    await hud.dismissAssistantOverlay();
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'navigation',
      navigation: destination,
    });
    expect(frames[frames.length - 1]).not.toHaveProperty('assistantOverlay');
  });

  it('restores the prior frame when a navigation paint and bounded heal both fail', async () => {
    const frames: HudVisualFrame[] = [];
    const results = [true, false, false, true];
    const visual = {
      paint: async (frame: HudVisualFrame) => {
        frames.push(frame);
        return results.shift() ?? true;
      },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    expect(await hud.showText('home before failed navigation', ContainerId.Main)).toBe(true);
    expect(await hud.showNavigation(navigationVisualFixture('nav-guidance'))).toBe(false);
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview', main: 'home before failed navigation',
    });
    expect(await hud.showText('home remains interactive', ContainerId.Main)).toBe(true);
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview', main: 'home remains interactive',
    });
  });

  it('restores the exact navigation frame after canonical-place confirmation', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    const destination = navigationVisualFixture('nav-destination');

    await hud.showNavigation(destination);
    await hud.showConfirmation('Navigate to Cajun Corner, 2301 N Lincoln Ave?', 'nav-place');
    await hud.dismissConfirmation('nav-place');

    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'navigation', navigation: destination,
    });
  });

  it('rejects a navigation view while confirmation owns the visible HUD', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    await hud.showConfirmation('Send this message?', 'active-confirmation');
    expect(await hud.showNavigation(navigationVisualFixture('nav-mode'))).toBe(false);
    await hud.dismissConfirmation('active-confirmation');

    expect(frames.some((frame) => frame.kind === 'navigation')).toBe(false);
  });

  it('defers a native reminder lifecycle page behind confirmation and restores it exactly', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    const reminder = {
      domain: 'reminders' as const,
      phase: 'saved' as const,
      title: 'Buy filters',
      primary: 'Saved and verified',
      secondary: 'Reminders read-back matched',
      hint: 'left x2 Reminders',
    };

    await hud.showConfirmation('Create reminder “Buy filters”? Personal', 'reminder-create');
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'confirmation', reviewDomain: 'reminders',
    });
    expect(await hud.showProductivity(reminder)).toBe(true);
    await hud.dismissConfirmation('reminder-create');

    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'productivity', productivity: reminder,
    });
  });

  it('keeps Mac-owned Overview regions blank during status-only boot', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    await hud.showText('4:28   JOAT connecting', ContainerId.StatusBar);

    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview',
      status: '4:28   JOAT connecting',
      main: '',
      context: '',
      tray: '',
    });
  });

  it('carries Mac urgency brightness into the custom visual frame', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    await hud.showText('routine', ContainerId.Main, 3);
    expect(frames[frames.length - 1]).toMatchObject({ mainBrightness: 3 });
    await hud.showText('urgent', ContainerId.Main, 4);
    expect(frames[frames.length - 1]).toMatchObject({ mainBrightness: 4 });
  });

  it('keeps mic privacy state structured instead of inferring it from status copy', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    hud.setMicState(false);
    await hud.showText('9:41   JOAT connecting', ContainerId.StatusBar);
    expect(frames[frames.length - 1]).toMatchObject({ micOn: false });

    hud.setMicState(true);
    await hud.showText('9:41   JOAT paused', ContainerId.StatusBar);
    expect(frames[frames.length - 1]).toMatchObject({ micOn: true });
  });

  it('passes an exact redacted Planning grid to the visual list and drops partial metadata', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    const planningGrid = Array.from({ length: 42 }, (_, index) => {
      const value = new Date(Date.UTC(2026, 7, 31 + index));
      return {
        day: value.toISOString().slice(0, 10),
        eventCount: index === 1 ? 1 : 0,
        reminderCount: index === 1 ? 1 : 0,
        inMonth: value.getUTCMonth() === 8,
      };
    });

    await hud.showList(['AUG 31–SEP 6 · 2 planned'], {
      title: 'PLANNING · PLAN · SEPTEMBER 2026',
      selectable: true,
      planningGrid,
    });
    const complete = frames[frames.length - 1]!;
    expect(complete.kind).toBe('list');
    if (complete.kind === 'list') {
      expect(complete.planningGrid).toEqual(planningGrid);
    }

    await hud.showList(['AUG 31–SEP 6 · 2 planned'], {
      title: 'PLANNING · PLAN · SEPTEMBER 2026',
      selectable: true,
      planningGrid: planningGrid.slice(0, 41),
    });
    const partial = frames[frames.length - 1]!;
    expect(partial.kind).toBe('list');
    if (partial.kind === 'list') expect(partial.planningGrid).toBeUndefined();
  });

  it('keeps a plain Mac reply and its appended stream in the visual overview frame', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    await hud.showText('Meeting starts in', ContainerId.Main, 4);
    vi.advanceTimersByTime(400);
    await hud.appendText(' 19 minutes.', ContainerId.Main);

    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview', main: 'Meeting starts in 19 minutes.',
    });
  });

  it('retains the Mac content kind that distinguishes an incoming card from a reply', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    await hud.showText(
      'Dele · Messages\nOn my way',
      ContainerId.Main,
      3,
      'notification',
    );

    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview',
      mainKind: 'notification',
      main: 'Dele · Messages\nOn my way',
    });
  });

  it('renders the transient sidebar over a dim snapshot, then full-focus Messages restores the latest overview', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showText('Next\nDesign review · 4:30', ContainerId.Main, 4);
    await hud.showText('Last query  nearest coffee shop?', ContainerId.Context, 3);
    await hud.showText('2 notifications', ContainerId.Tray, 2);

    await hud.showSidebar(['Messages', 'Notifications', 'Calendar', 'Debate', 'Settings']);
    const sidebar = rebuiltPages[0] as {
      textObject?: Array<{ containerID?: number; content?: string }>;
      listObject?: Array<{ width?: number }>;
    };
    expect(sidebar.listObject?.[0]?.width).toBe(160);
    expect(sidebar.textObject?.find((c) => c.containerID === ContainerId.Main)?.content)
      .toBe('Next\nDesign review · 4:30');

    await hud.showText('Updated overview behind navigation', ContainerId.Main, 3);
    await hud.showList(['Loading recent messages…'], {
      title: 'MESSAGES', hint: 'left x2 overview',
    });
    const messages = rebuiltPages[1] as {
      textObject?: Array<{ containerID?: number; content?: string }>;
      listObject?: Array<{ width?: number }>;
    };
    expect(messages.listObject?.[0]?.width).toBe(CANVAS.width);
    expect(messages.textObject?.find((c) => c.containerID === ContainerId.Main)?.content)
      .toBe('MESSAGES');

    await hud.showDefault();
    expect(lastOf(mainPaints()).content).toBe('Updated overview behind navigation');
  });

  it('paints a named focus page with a persistent truthful action hint', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showList(['Messages', 'Settings'], {
      title: 'CAPABILITIES',
      hint: 'tap open · left x2 back',
    });

    const page = rebuiltPages[0] as {
      textObject?: Array<{ containerID?: number; content?: string; textColor?: number }>;
    };
    expect(page.textObject?.find((c) => c.containerID === ContainerId.Main)).toMatchObject({
      content: 'CAPABILITIES', textColor: 4,
    });
    expect(page.textObject?.find((c) => c.containerID === ContainerId.Hint)).toMatchObject({
      content: 'tap open · left x2 back', textColor: 1,
    });

    // Chrome repaints its home hint on every state/clock change. In a focus
    // page that write is intentionally ignored so the active gesture map
    // remains truthful and list movement cannot revert it.
    await hud.showText('left double-tap: menu / back · say “JOAT …”', ContainerId.Hint);
    expect(paints.filter((p) => p.containerID === ContainerId.Hint)).toEqual([]);
  });

  it('repaints the live status immediately across focus and confirmation rebuilds', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showText('12:34   JOAT listening   bat 80%', ContainerId.StatusBar, 2);
    await hud.showList(['Messages'], {
      title: 'CAPABILITIES',
      hint: 'tap open · left x2 back',
    });
    expect(lastOf(paints.filter((p) => p.containerID === ContainerId.StatusBar))).toMatchObject({
      content: '12:34   JOAT listening   bat 80%', textColor: 2,
    });

    await hud.showConfirmation('Send this message?', 'status-proof');
    expect(lastOf(paints.filter((p) => p.containerID === ContainerId.StatusBar))).toMatchObject({
      content: '12:34   JOAT listening   bat 80%', textColor: 2,
    });
  });

  it('keeps dashboard paints behind focus, then restores their latest content', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showList(['Messages'], {
      title: 'CAPABILITIES',
      hint: 'tap open · left x2 back',
    });

    await hud.showText('background reply', ContainerId.Main);
    await hud.showText('background transcript', ContainerId.Context);
    await hud.showText('background queue', ContainerId.Tray);
    await hud.showText('dashboard hint', ContainerId.Hint);
    await hud.appendText('stream tail', ContainerId.Main);

    expect(paints).toEqual([]);
    expect(rebuilds).toBe(1);

    await hud.showDefault();
    expect(lastOf(mainPaints()).content).toBe('background replystream tail');
    expect(lastOf(paints.filter((p) => p.containerID === ContainerId.Context)).content)
      .toBe('background transcript');
    expect(lastOf(paints.filter((p) => p.containerID === ContainerId.Tray)).content)
      .toBe('background queue');
    expect(lastOf(paints.filter((p) => p.containerID === ContainerId.Hint)).content)
      .toBe('dashboard hint');
    expect(rebuilds).toBe(2);
  });

  it('confirmation is exclusive and restores content received while it was open', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showText('previous reply', ContainerId.Main, 4);
    await hud.showText('you  delete the file', ContainerId.Context, 3);

    await hud.showConfirmation('Delete report.pdf permanently?');
    const confirmation = rebuiltPages[0] as {
      textObject?: Array<{ containerID?: number; content?: string; textColor?: number }>;
    };
    expect(confirmation.textObject?.find((c) => c.containerID === ContainerId.Main)).toMatchObject({
      content: 'CONFIRM ACTION\n\nDelete report.pdf permanently?',
      textColor: 4,
    });
    expect(confirmation.textObject?.find((c) => c.containerID === ContainerId.Context)).toMatchObject({
      content: 'TAP · confirm   SWIPE · cancel', textColor: 3,
    });
    expect(confirmation.textObject?.find((c) => c.containerID === ContainerId.Tray)).toMatchObject({
      content: '', textColor: 3,
    });
    expect(confirmation.textObject?.find((c) => c.containerID === ContainerId.Hint)).toMatchObject({
      content: '8 seconds · timeout cancels', textColor: 1,
    });

    await hud.showText('reply completed behind the gate', ContainerId.Main, 3);
    expect(lastOf(mainPaints()).content).toBe('previous reply');

    await hud.dismissConfirmation();
    expect(lastOf(mainPaints())).toMatchObject({
      content: 'reply completed behind the gate', textColor: 3,
    });
    expect(lastOf(paints.filter((p) => p.containerID === ContainerId.Context))).toMatchObject({
      content: 'you  delete the file', textColor: 3,
    });
  });

  it('serializes confirmation paints for the single SDK bridge', async () => {
    let activePaints = 0;
    let overlapped = false;
    const client = {
      require: () => ({
        textContainerUpgrade: async () => {
          activePaints += 1;
          if (activePaints > 1) overlapped = true;
          await Promise.resolve();
          activePaints -= 1;
          return true;
        },
        rebuildPageContainer: async () => true,
      }),
    } as unknown as BridgeClient;
    const hud = new HudPresenter(client, new HudPageBuilder());

    await hud.showConfirmation('Send this message?');

    expect(overlapped).toBe(false);
  });

  it('fails closed instead of truncating an over-capacity exact preview', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());

    expect(await hud.showConfirmation('🥽'.repeat(61))).toBe(false);
    expect(mainPaints()).toHaveLength(0);
  });

  it('does not trap later content when a focus-to-confirm rebuild fails', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showList(['Messages'], {
      title: 'CAPABILITIES',
      hint: 'tap open · left x2 back',
    });
    rebuildOk = false;

    expect(await hud.showConfirmation('Send this message?')).toBe(false);
    rebuildOk = true;
    expect(await hud.showDefault()).toBe(true);
    await hud.showText('dashboard write after failed modal', ContainerId.Main);

    expect(lastOf(mainPaints()).content).toBe('dashboard write after failed modal');
  });

  it('does not trap later content when the first visual confirmation frame fails', async () => {
    const frames: HudVisualFrame[] = [];
    const results = [true, false, true];
    const visual = {
      paint: async (frame: HudVisualFrame) => {
        frames.push(frame);
        return results.shift() ?? true;
      },
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    expect(await hud.showText('home', ContainerId.Main)).toBe(true);
    rebuildOk = false;
    expect(await hud.showConfirmation('Send this message?', 'visual-failure')).toBe(false);
    rebuildOk = true;
    expect(await hud.showText('after failed gate', ContainerId.Main)).toBe(true);

    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview', main: 'after failed gate',
    });
    expect(vi.getTimerCount()).toBe(0);
  });

  it('actively restores the prior complete frame after a partial confirmation upload', async () => {
    const updates: number[] = [];
    const rendered: HudVisualFrame[] = [];
    const client = {
      require: () => ({
        updateImageRawData: async (update: { containerID?: number }) => {
          updates.push(update.containerID ?? -1);
          // Home is calls 1-4. Fail the second tile in both the first
          // confirmation paint and its one bounded self-heal retry.
          return updates.length === 6 || updates.length === 8
            ? ImageRawDataUpdateResult.sendFailed
            : ImageRawDataUpdateResult.success;
        },
        rebuildPageContainer: async () => true,
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async (frame) => {
        rendered.push(frame);
        return [ContainerId.Image, 8, 9, 10].map((containerId) => ({
          containerId,
          pixels: new Uint8Array(4),
        }));
      },
    };
    const hud = new HudPresenter(
      client,
      new HudPageBuilder(),
      new HudVisualSurface(client, renderer),
    );

    expect(await hud.showText('home', ContainerId.Main)).toBe(true);
    expect(await hud.showConfirmation('Send this message?', 'partial-frame')).toBe(false);

    expect(updates.slice(-4)).toEqual([ContainerId.Image, 8, 9, 10]);
    expect(rendered[rendered.length - 1]).toMatchObject({ kind: 'overview', main: 'home' });
    expect(vi.getTimerCount()).toBe(0);
  });

  it('starts the visual countdown only after the first complete frame lands', async () => {
    let finishPaint: ((painted: boolean) => void) | undefined;
    const visual = {
      paint: (frame: HudVisualFrame) => frame.kind === 'confirmation'
        ? new Promise<boolean>((resolve) => { finishPaint = resolve; })
        : Promise.resolve(true),
    } as HudVisualSurface;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    const opening = hud.showConfirmation('Send this message?', 'timing-proof');
    await Promise.resolve();
    expect(vi.getTimerCount()).toBe(0);
    expect(finishPaint).toBeDefined();

    finishPaint?.(true);
    expect(await opening).toBe(true);
    expect(vi.getTimerCount()).toBe(1);

    await hud.dismissConfirmation('timing-proof');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('confirmation opened from a focus page returns to that page', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showList(['instant', 'ask me first'], {
      title: 'LISTENING RESUME',
      hint: 'tap apply · left x2 back',
    });
    await hud.showConfirmation('Resume listening?');
    await hud.dismissConfirmation();

    expect(rebuilds).toBe(3); // list → confirmation dashboard → restored list
    expect(rebuiltPages).toHaveLength(3);
    const restored = rebuiltPages[2] as {
      textObject?: Array<{ containerID?: number; content?: string }>;
    };
    expect(restored.textObject?.find((c) => c.containerID === ContainerId.Main)?.content)
      .toBe('LISTENING RESUME');
    expect(restored.textObject?.find((c) => c.containerID === ContainerId.Hint)?.content)
      .toBe('tap apply · left x2 back');
  });

  it('ignores a stale dismissal after a replacement action claims the modal', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showConfirmation('Approve action A?', 'action-a');

    const replacement = hud.showConfirmation('Approve action B?', 'action-b');
    const staleDismissal = hud.dismissConfirmation('action-a');
    const [, staleResult] = await Promise.all([replacement, staleDismissal]);

    expect(staleResult).toBe(false);

    const active = rebuiltPages[rebuiltPages.length - 1] as {
      textObject?: Array<{ containerID?: number; content?: string }>;
    };
    expect(active.textObject?.find((c) => c.containerID === ContainerId.Main)?.content)
      .toBe('CONFIRM ACTION\n\nApprove action B?');
    await hud.dismissConfirmation('action-b');
    const home = rebuiltPages[rebuiltPages.length - 1] as {
      textObject?: Array<{ containerID?: number; content?: string }>;
    };
    expect(home.textObject?.find((c) => c.containerID === ContainerId.Main)?.content).toBe('');
  });
});


describe('HudPresenter self-heals a missing page', () => {
  /**
   * createStartUpPageContainer is one-shot PER HOST SESSION, so after a
   * WebView reload it returns `invalid`, BridgeClient's rebuild fallback
   * reports success without materialising containers, and every paint then
   * failed silently — 382 consecutive "container not found" warnings behind
   * a blank display in the simulator, with no recovery for the session.
   */
  it('rebuilds and repaints when a paint reports the container is gone', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    paintOk = false;
    const ok = await hud.showText('hello', ContainerId.Main);
    expect(rebuilds).toBe(1);
    expect(ok).toBe(true);
    expect(lastOf(mainPaints()).content).toBe('hello'); // the text landed
  });

  it('gives up after ONE rebuild — never recurses', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    paintOk = false;
    rebuildOk = false; // rebuild does not fix it
    const ok = await hud.showText('hello', ContainerId.Main);
    expect(ok).toBe(false);
    expect(rebuilds).toBe(1);
  });

  it('a healthy paint never rebuilds', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showText('fine', ContainerId.Main);
    expect(rebuilds).toBe(0);
  });

  it('records the one permitted self-heal decision sequence', async () => {
    const events: unknown[] = [];
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), undefined, {
      record: (event) => events.push(event),
    });
    paintOk = false;

    await hud.showText('hello', ContainerId.Main);

    expect(events).toEqual([
      { type: 'self_heal', surface: 'overview', target: 'text', decision: 'attempt', containerId: 2 },
      { type: 'self_heal', surface: 'overview', target: 'text', decision: 'recovered', containerId: 2 },
    ]);
  });
});

describe('HudPresenter diagnostic page transitions', () => {
  it('records the layout transition but never list text', async () => {
    const events: unknown[] = [];
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), undefined, {
      record: (event) => events.push(event),
    });

    await hud.showList(['A private message body'], { title: 'MESSAGES' });

    expect(events).toEqual([{
      type: 'page_transition',
      from: 'overview', to: 'list', reason: 'show_list', outcome: 'success',
    }]);
  });
});
