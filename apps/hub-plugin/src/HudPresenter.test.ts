import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ImageRawDataUpdateResult,
  type RebuildPageContainer,
  type TextContainerUpgrade,
} from '@evenrealities/even_hub_sdk';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import {
  HudVisualSurface,
  type HudFramePainter,
  type HudFrameRenderer,
  type HudVisualFrame,
} from './HudVisualSurface';
import { CANVAS, ContainerId } from './types';
import { navigationVisualFixture } from './NavigationHud';

// These tests pin the IMAGE overview (VITE_NATIVE_OVERVIEW=0). The text
// overview, which is Home by default, has its own: HudPresenter.textOverview.test.ts.
setNativeOverviewForTest(false);



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

describe('prompt question ownership', () => {
  function show(hud: HudPresenter, id: string, question: string, isCurrent = () => true) {
    return hud.showPromptQuestion(id, question, isCurrent);
  }
  function rig(paint: (value: HudVisualFrame) => Promise<boolean>) {
    const visual = {
      paint,
      paintExact: async (value: HudVisualFrame, isCurrent: () => boolean) => {
        if (!isCurrent()) return false;
        const ok = await paint(value);
        return ok && isCurrent();
      },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    };
    return new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
  }

  it('publishes a terminal question inline only after its exact paint and restores the conversation on cancel', async () => {
    // The Agent Terminal is Even's own two-text page (2026-09-13): the
    // question rides the terminal body as one serialized text upgrade, so
    // the exact paint to wait for is that bridge write, not a raster frame.
    const upgrades: TextContainerUpgrade[] = [];
    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    let entered = false;
    const client = {
      require: () => ({
        rebuildPageContainer: async () => true,
        textContainerUpgrade: async (upgrade: TextContainerUpgrade) => {
          upgrades.push(upgrade);
          if (upgrade.content?.includes('QUESTION >')) { entered = true; await held; }
          return true;
        },
      }),
    } as unknown as BridgeClient;
    const hud = new HudPresenter(client, new HudPageBuilder(), {
      paint: async () => true,
      paintExact: async (_value: HudVisualFrame, isCurrent: () => boolean) => isCurrent(),
      forgetPage: () => undefined,
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    });
    const body = () => lastOf(upgrades.filter(u => u.containerName === 'terminal-body')).content ?? '';
    const model = {screen: 'agent_terminal' as const, state: 'ready' as const,
      topic: 'OpenClaw · ready', collaborators: [], activity: '', conversationId: 'conversation-a',
      messages: [{id:'answer-one', role:'assistant' as const, text:'Previous answer remains visible.'}]};
    expect(await hud.showBrainstorm(model)).toBe(true);
    let settled = false;
    const publication = hud.showPromptQuestion('terminal-prompt', 'Speak to OpenClaw.', () => true, 'terminal')
      .then(ok => { settled = true; return ok; });
    await vi.waitFor(() => expect(entered).toBe(true));
    expect(settled).toBe(false);
    expect(body()).toContain('QUESTION > Speak to OpenClaw.');
    expect(body()).toContain('Previous answer remains visible.');
    release(); expect(await publication).toBe(true);
    expect(await hud.dismissPromptQuestion('terminal-prompt')).toBe(true);
    expect(body()).not.toContain('QUESTION >');
    expect(body()).toContain('Previous answer remains visible.');
  });

  it('rejects terminal publication when its conversation is absent', async () => {
    const hud = rig(async () => true);
    expect(await hud.showPromptQuestion('terminal-prompt', 'Speak to Hermes.', () => true, 'terminal')).toBe(false);
  });


  it('reports failure while confirmation owns the visible surface', async () => {
    const frames: HudVisualFrame[] = [];
    const hud = rig(async (value) => { frames.push(value); return true; });
    expect(await hud.showConfirmation('Send the reviewed message?', 'confirm-a')).toBe(true);
    expect(await show(hud, 'prompt-a', 'Say the message.')).toBe(false);
    await hud.dismissConfirmation('confirm-a');
    expect(frames[frames.length - 1]).toMatchObject({ kind: 'overview' });
  });

  it('refuses a question on a visual surface that cannot hand its page over', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), { paint: async () => true });
    expect(await show(hud, 'prompt-a', 'Say the message.')).toBe(false);
  });


});

it('shows streamed replies on the native text fallback and restores the retained page', async () => {
  const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
  await hud.showText('Retained page', ContainerId.Main);
  expect(await hud.showAssistantOverlay('First turn')).toBe(true);
  expect(lastOf(mainPaints()).content).toBe('First turn');
  expect(hud.hasAssistantOverlay()).toBe(true);

  await hud.showAssistantOverlay(' preserved', true);
  await vi.advanceTimersByTimeAsync(500);
  expect(lastOf(mainPaints()).content).toBe('First turn preserved');

  await hud.showText('Updated page', ContainerId.Main);
  expect(lastOf(mainPaints()).content).toBe('First turn preserved');
  expect(hud.settleAssistantOverlay(1000)).toBe(true);
  await vi.advanceTimersByTimeAsync(1000);
  expect(hud.hasAssistantOverlay()).toBe(false);
  expect(lastOf(mainPaints()).content).toBe('Updated page');
  expect(lastOf(mainPaints()).contentLength).toBe('First turn preserved'.length);
});

it('keeps native text replies behind confirmation until the exact gate is dismissed', async () => {
  const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
  await hud.showText('Retained page', ContainerId.Main);
  await hud.showAssistantOverlay('First turn');
  await hud.showAssistantOverlay(' pending', true);
  await hud.showConfirmation('Send this message?', 'send-1');
  const paintsBeforeReply = paints.length;

  expect(await hud.showAssistantOverlay('Deferred reply')).toBe(true);
  await vi.advanceTimersByTimeAsync(500);
  expect(paints).toHaveLength(paintsBeforeReply);
  expect(await hud.dismissConfirmation('stale-gate')).toBe(false);
  expect(paints).toHaveLength(paintsBeforeReply);

  expect(await hud.dismissConfirmation('send-1')).toBe(true);
  expect(lastOf(mainPaints()).content).toBe('Deferred reply');
  await hud.dismissAssistantOverlay();
  expect(lastOf(mainPaints()).content).toBe('Retained page');
});

beforeEach(() => {
  vi.useFakeTimers();
});

it.each([false, true])('settles an in-flight native reply before actionable confirmation (reject=%s)', async (reject) => {
  let finishReply!: () => void;
  let replyDispatched!: () => void;
  const dispatched = new Promise<void>((resolve) => { replyDispatched = resolve; });
  let visible = '';
  let rebuilds = 0;
  const client = {
    require: () => ({
      textContainerUpgrade: async (update: TextContainerUpgrade) => {
        if (update.containerID === ContainerId.Main) {
          replyDispatched();
          await new Promise<void>((resolve, fail) => {
            finishReply = () => reject ? fail(new Error('native reply failed')) : resolve();
          });
          visible = update.content ?? '';
        }
        return true;
      },
      rebuildPageContainer: async (page: { textObject: Array<{ containerID: number; content: string }> }) => {
        rebuilds += 1;
        visible = page.textObject.find((item) => item.containerID === ContainerId.Main)?.content ?? '';
        return true;
      },
    }),
  } as unknown as BridgeClient;
  const hud = new HudPresenter(client, new HudPageBuilder());
  const reply = hud.showAssistantOverlay('Old reply').catch(() => false);
  // The reply yields to the native-terminal handoff before its write; the
  // gate under test is the one that finds that write already on the bridge.
  await dispatched;
  const confirmation = hud.showConfirmation('Send this message?', 'send-1');
  await vi.advanceTimersByTimeAsync(500);
  expect(rebuilds).toBe(0);
  finishReply();
  expect(await reply).toBe(!reject);
  expect(await confirmation).toBe(true);
  expect(visible).toBe('CONFIRM ACTION\n\nSend this message?');
});

it('gives a native list reply a reading area and restores the retained list', async () => {
  const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
  const items = ['First item', 'Second item'];
  await hud.showList(items, { title: 'NEWS' });
  await hud.showAssistantOverlay('A reply with several lines\nthat needs more than the list title.');
  const replyPage = rebuiltPages[rebuiltPages.length - 1] as { textObject: Array<{ containerID: number; height: number }>; listObject?: unknown[] };
  expect(replyPage.textObject.find((item) => item.containerID === ContainerId.Main)?.height).toBeGreaterThan(40);
  expect(replyPage.listObject ?? []).toHaveLength(0);
  expect(lastOf(mainPaints()).content).toContain('several lines');
  const built = rebuilds;
  await hud.showAssistantOverlay(' Streamed tail.', true);
  await vi.advanceTimersByTimeAsync(500);
  expect(rebuilds).toBe(built);

  await hud.showConfirmation('Send this message?', 'send-1');
  await hud.dismissConfirmation('send-1');
  expect(lastOf(mainPaints()).content).toContain('Streamed tail.');
  await hud.dismissAssistantOverlay();
  const restored = rebuiltPages[rebuiltPages.length - 1] as { listObject: Array<{ itemContainer: { itemName: string[] } }> };
  expect(restored.listObject[0]?.itemContainer.itemName).toEqual(items);
  expect(hud.hasAssistantOverlay()).toBe(false);
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
    // A text container holds at most 999 UTF-8 bytes (Even simulator README
    // v0.7.1, "to behave more like firmware"); an upgrade beyond that is
    // rejected whole. The cut is marked with an ellipsis inside the budget.
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.appendText('x'.repeat(1990));
    vi.advanceTimersByTime(400);
    await hud.appendText('THE-END-' + 'y'.repeat(100));
    vi.advanceTimersByTime(400);
    const last = lastOf(mainPaints());
    expect(new TextEncoder().encode(last.content!).length).toBeLessThanOrEqual(999);
    expect(last.content!.startsWith('…')).toBe(true);
    expect(last.content!.endsWith('y'.repeat(100))).toBe(true);
  });

  it('status bar and main stream independently', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    hud.setMicState(true);
    await hud.appendText('reply text');
    await hud.setStatus('9:41 JOAT · listening bat 80%');
    vi.advanceTimersByTime(400);
    expect(lastOf(mainPaints()).content).toBe('reply text');
    const status = paints.filter((p) => p.containerID === ContainerId.StatusBar);
    // The text compatibility top line is the structured clock · mic ·
    // battery triple; the activity word stays in the conversation.
    expect(lastOf(status).content).toBe('9:41 · MIC ON · bat 80%');
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
    // The surface settles its single-writer barrier before rendering, so
    // wait for the old frame to reach the renderer rather than counting
    // microtasks; the newer frame must then queue behind its blocked upload.
    await vi.waitFor(() => expect(rendered).toEqual(['old generation']));
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
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
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

  it('composites a receipt strip over the live map, then removes it without replacing navigation', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    const destination = navigationVisualFixture('nav-guidance');

    await hud.showNavigation(destination);
    // An answer would be a firmware text page; a receipt is a strip on the
    // image page, so the map stays beneath it.
    await hud.showAssistantOverlay('Reminder saved · Buy filters', false, true, 'receipt');

    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'navigation',
      navigation: destination,
      assistantOverlay: { text: 'Reminder saved · Buy filters', presentation: 'receipt' },
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
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
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
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
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
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    await hud.showConfirmation('Send this message?', 'active-confirmation');
    expect(await hud.showNavigation(navigationVisualFixture('nav-mode'))).toBe(false);
    await hud.dismissConfirmation('active-confirmation');

    expect(frames.some((frame) => frame.kind === 'navigation')).toBe(false);
  });

  it('defers a saved receipt behind confirmation and restores the page with the strip', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    const reminder = {
      domain: 'reminders' as const,
      phase: 'saved' as const,
      title: 'Buy filters',
      primary: 'Saved and verified',
      secondary: 'Reminders read-back matched',
      hint: '2x tap: Reminders',
    };

    expect(await hud.showConfirmation('Create reminder “Buy filters”? Personal', 'reminder-create')).toBe(true);
    expect(await hud.showProductivity(reminder)).toBe(true);
    await hud.dismissConfirmation('reminder-create');

    // Transient outcome = strip over the restored page, not a page swap.
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview', actionOverlay: { productivity: reminder },
    });
    expect(hud.isProductivityVisible(reminder)).toBe(true);
  });


  it('never draws a SAVED strip over a retained FAILED page', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    const failed = {
      domain: 'calendar' as const,
      phase: 'failed' as const,
      title: 'Design review',
      primary: 'EventKit refused the write',
      secondary: 'Safe to retry',
      hint: '2x tap: Planning',
    };
    const saved = { ...failed, phase: 'saved' as const, primary: 'Saved and verified' };

    // Direct retry: saved arrives while the failed page is still on glass.
    await hud.showProductivity(failed);
    await hud.showProductivity(saved);
    const direct = frames[frames.length - 1];
    expect(direct?.kind).not.toBe('productivity');
    expect(direct).toMatchObject({ actionOverlay: { productivity: saved } });

    // Deferred retry: the gate captured the failed page; dismissal must not
    // resurrect it beneath the newer receipt.
    await hud.showProductivity(failed);
    await hud.showConfirmation('Create event “Design review”? Work', 'retry-create');
    await hud.showProductivity(saved);
    await hud.dismissConfirmation('retry-create');
    const restored = frames[frames.length - 1];
    expect(restored?.kind).not.toBe('productivity');
    expect(restored).toMatchObject({ actionOverlay: { productivity: saved } });
  });

  it('keeps the receipt strip across route progress updates but not page changes', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    const saved = {
      domain: 'calendar' as const,
      phase: 'saved' as const,
      title: 'Design review',
      primary: 'Saved and verified',
      secondary: 'Calendar read-back matched',
      hint: '2x tap: Planning',
    };

    await hud.showNavigation(navigationVisualFixture('nav-guidance'));
    expect(await hud.showProductivity(saved)).toBe(true);
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'navigation', actionOverlay: { productivity: saved },
    });

    // Progress frame: same page updating — the strip rides along.
    await hud.showNavigation(navigationVisualFixture('nav-guidance'));
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'navigation', actionOverlay: { productivity: saved },
    });

    // A different page supersedes it.
    await hud.showList(['[09:30] Design review'], { title: 'PLANNING', selectable: true });
    expect(hud.isProductivityVisible(saved)).toBe(false);
  });

  it('supersedes the receipt strip when the next page arrives', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    const saved = {
      domain: 'calendar' as const,
      phase: 'saved' as const,
      title: 'Design review',
      primary: 'Saved and verified',
      secondary: 'Calendar read-back matched',
      hint: '2x tap: Planning',
    };

    expect(await hud.showProductivity(saved)).toBe(true);
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview', actionOverlay: { productivity: saved },
    });
    await hud.showList(['[09:30] Design review'], { title: 'PLANNING', selectable: true });
    expect(hud.isProductivityVisible(saved)).toBe(false);
  });


  it('does not confuse a distinct verifying presentation receipt with the retained surface', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    const verifying = {
      domain: 'calendar' as const,
      phase: 'verifying' as const,
      title: 'Private event',
      primary: 'Saving calendar event',
      secondary: 'Checking native read-back',
      hint: 'Please wait',
      presentationReceiptId: 'a76b12f1-45e4-41fd-aed8-086f6b6b1954',
      presentationReceiptGeneration: 7,
    };

    await hud.showProductivity(verifying);

    expect(hud.isProductivityVisible(verifying)).toBe(true);
    expect(hud.isProductivityVisible({
      ...verifying,
      presentationReceiptId: '67ec97c5-a0fd-48ec-89cb-1b756ddaf87a',
    })).toBe(false);
  });

  it('keeps Mac-owned Overview regions blank during status-only boot', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
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
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
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
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    hud.setMicState(false);
    await hud.showText('9:41   JOAT connecting', ContainerId.StatusBar);
    expect(frames[frames.length - 1]).toMatchObject({ micOn: false });

    hud.setMicState(true);
    await hud.showText('9:41   JOAT paused', ContainerId.StatusBar);
    expect(frames[frames.length - 1]).toMatchObject({ micOn: true });
  });

  it('passes an exact redacted Planning grid to the visual list; a partial grid is a firmware list', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
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
    // Without a whole grid there is nothing to draw: the page is Even's list.
    expect(frames[frames.length - 1]).toBe(complete);
  });

  it('keeps a plain Mac reply and its appended stream in the visual overview frame', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
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
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
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

  it('locally dismisses a visible Home notification and restores the displaced card', async () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);

    await hud.showText('NEXT\nDesign review · 4:30', ContainerId.Main, 4, 'calendar');
    await hud.showText('Dele · Messages\nOn my way', ContainerId.Main, 3, 'notification');

    expect(hud.hasVisibleNotification()).toBe(true);
    expect(await hud.dismissVisibleNotification()).toBe(true);
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview',
      main: 'NEXT\nDesign review · 4:30',
      mainKind: 'calendar',
    });
    expect(hud.hasVisibleNotification()).toBe(false);
  });

  it('a full-focus list buffers overview content and Home restores the latest', async () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    await hud.showText('Next\nDesign review · 4:30', ContainerId.Main, 4);

    await hud.showList(['Loading recent messages…'], {
      title: 'MESSAGES', hint: 'left x2 overview',
    });
    const messages = rebuiltPages[0] as {
      textObject?: Array<{ containerID?: number; content?: string }>;
      listObject?: Array<{ width?: number }>;
    };
    expect(messages.listObject?.[0]?.width).toBe(CANVAS.width);
    // The page's name is the header line (container 1); there is no title
    // container any more.
    expect(messages.textObject?.find((c) => c.containerID === ContainerId.StatusBar)?.content)
      .toBe('MESSAGES');
    expect(messages.textObject?.find((c) => c.containerID === ContainerId.Main)).toBeUndefined();

    // Content arriving behind the focus page must not be lost.
    await hud.showText('Updated overview behind messages', ContainerId.Main, 3);
    await hud.showDefault();
    expect(lastOf(mainPaints()).content).toBe('Updated overview behind messages');
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
    expect(page.textObject?.find((c) => c.containerID === ContainerId.StatusBar)).toMatchObject({
      content: 'CAPABILITIES', textColor: 2,
    });
    // The page builder rewrites gesture wording to the shared GesturePolicy
    // vocabulary ("left x2" → "2x tap") so every footer names the
    // same Back gesture.
    expect(page.textObject?.find((c) => c.containerID === ContainerId.Hint)).toMatchObject({
      content: 'tap open · 2x tap back', textColor: 1,
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
    // The compatibility top line carries the structured clock · mic ·
    // battery triple with the Mac's brightness; the activity word is not
    // inferred from it.
    expect(lastOf(paints.filter((p) => p.containerID === ContainerId.StatusBar))).toMatchObject({
      content: '12:34 · MIC OFF · bat 80%', textColor: 2,
    });

    await hud.showConfirmation('Send this message?', 'status-proof');
    expect(lastOf(paints.filter((p) => p.containerID === ContainerId.StatusBar))).toMatchObject({
      content: '12:34 · MIC OFF · bat 80%', textColor: 2,
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
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
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

  it('shows the gate as a firmware text page on the real surface, so no partial tile can tear it', async () => {
    const updates: number[] = [];
    const pages: Array<{ images: number; texts: number }> = [];
    const client = {
      require: () => ({
        updateImageRawData: async (update: { containerID?: number }) => {
          updates.push(update.containerID ?? -1);
          return ImageRawDataUpdateResult.success;
        },
        rebuildPageContainer: async (page: { imageObject?: unknown[]; textObject?: unknown[] }) => {
          pages.push({ images: page.imageObject?.length ?? 0, texts: page.textObject?.length ?? 0 });
          return true;
        },
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [ContainerId.Image, 8, 9, 10].map((containerId) => ({
        containerId,
        pixels: new Uint8Array(4),
      })),
    };
    const hud = new HudPresenter(
      client,
      new HudPageBuilder(),
      new HudVisualSurface(client, renderer),
    );

    expect(await hud.showText('home', ContainerId.Main)).toBe(true);
    const tilesBeforeGate = updates.length;
    expect(await hud.showConfirmation('Send this message?', 'native-gate')).toBe(true);

    // The gate itself is one text-only rebuild; it uploads no image tile.
    expect(pages[pages.length - 1]).toEqual({ images: 0, texts: 5 });
    expect(updates).toHaveLength(tilesBeforeGate);

    expect(await hud.dismissConfirmation('native-gate')).toBe(true);
    // Back to the image page: the four containers return, then Home repaints.
    expect(pages[pages.length - 1]).toEqual({ images: 4, texts: 1 });
    expect(updates.slice(-4)).toEqual([ContainerId.Image, 8, 9, 10]);
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
    expect(restored.textObject?.find((c) => c.containerID === ContainerId.StatusBar)?.content)
      .toBe('LISTENING RESUME');
    expect(restored.textObject?.find((c) => c.containerID === ContainerId.Hint)?.content)
      .toBe('tap apply · 2x tap back');
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

  it('fails closed for an accepted action replaced while its dismissal repaints', async () => {
    let holdDismissalRebuild = false;
    let releaseDismissalRebuild: (() => void) | undefined;
    let dismissalRebuildStarted: (() => void) | undefined;
    const dismissalRebuildStartedPromise = new Promise<void>((resolve) => {
      dismissalRebuildStarted = resolve;
    });
    const client = {
      require: () => ({
        textContainerUpgrade: async (paint: TextContainerUpgrade) => {
          paints.push(paint);
          return true;
        },
        rebuildPageContainer: async (page: unknown) => {
          rebuilds += 1;
          rebuiltPages.push(page);
          if (holdDismissalRebuild) {
            holdDismissalRebuild = false;
            dismissalRebuildStarted?.();
            await new Promise<void>((resolve) => {
              releaseDismissalRebuild = resolve;
            });
          }
          return true;
        },
      }),
    } as unknown as BridgeClient;
    const hud = new HudPresenter(client, new HudPageBuilder());
    await hud.showConfirmation('Approve action A?', 'action-a');

    holdDismissalRebuild = true;
    const dismissA = hud.dismissConfirmation('action-a');
    await dismissalRebuildStartedPromise;
    const replacement = hud.showConfirmation('Approve action B?', 'action-b');
    releaseDismissalRebuild?.();

    await expect(dismissA).resolves.toBe(false);
    await expect(replacement).resolves.toBe(true);
    const active = rebuiltPages[rebuiltPages.length - 1] as {
      textObject?: Array<{ containerID?: number; content?: string }>;
    };
    expect(active.textObject?.find((c) => c.containerID === ContainerId.Main)?.content)
      .toBe('CONFIRM ACTION\n\nApprove action B?');
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

describe('HudPresenter keeps the last frame when the link drops a send', () => {
  /**
   * A frame the host could not deliver (sendFailed, or out of time) is not a
   * missing page. Rebuilding for it wiped the last good frame on every frame
   * of a weak link; only an image the host turned down calls for it.
   */
  it.each([
    ['could not deliver', 'transport', 0],
    ['turned down', 'refused', 1],
  ] as const)('a frame the host %s rebuilds the page %i times', async (_name, failure, expected) => {
    let pageRebuilds = 0;
    const client = {
      require: () => ({
        rebuildPageContainer: vi.fn(async () => { pageRebuilds += 1; return true; }),
        textContainerUpgrade: vi.fn(async () => true),
      }),
    } as unknown as BridgeClient;
    const visual = {
      paint: vi.fn(async () => false),
      lastPaintFailure: () => failure,
      forgetPage: vi.fn(),
      whenIdle: vi.fn(async () => true),
      withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
    };
    const hud = new HudPresenter(client, new HudPageBuilder(), visual);
    expect(await hud.showText('home', ContainerId.Main)).toBe(false);
    expect(pageRebuilds).toBe(expected);
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

describe('HudPresenter background migration', () => {
  it('carries the wearer\'s open page across the host\'s WebView reload', async () => {
    // Backgrounding builds a NEW page and replays a JSON snapshot into it, so
    // this asserts across two presenters — not a save/load on one object.
    const before = new HudPresenter(fakeClient(), new HudPageBuilder());
    await before.showList(['Client standup', 'Field walkthrough'], {
      title: 'QUICKNOTES',
      hint: 'tap: detail · 2x tap: back',
      selectable: true,
    });
    before.setMicState(true);

    const wire = JSON.parse(JSON.stringify(before.exportBackgroundState()));

    const after = new HudPresenter(fakeClient(), new HudPageBuilder());
    expect(after.importBackgroundState(wire)).toBe(true);

    // The restored page shows what the wearer was looking at, not Overview.
    const restored = after.exportBackgroundState() as {
      surface: { kind: string; items?: string[] };
      micOn: boolean;
    };
    expect(restored.surface.kind).toBe('list');
    expect(restored.surface.items).toEqual(['Client standup', 'Field walkthrough']);
    expect(restored.micOn).toBe(true);
  });

  it('survives the JSON round trip that Maps do not', async () => {
    const before = new HudPresenter(fakeClient(), new HudPageBuilder());
    await before.showText('Your meeting starts in 19 minutes.', ContainerId.Main);

    const wire = JSON.parse(JSON.stringify(before.exportBackgroundState()));
    // Container-keyed Maps become plain records or they vanish in transit.
    expect(wire.streams[String(ContainerId.Main)]).toContain('19 minutes');

    const after = new HudPresenter(fakeClient(), new HudPageBuilder());
    after.importBackgroundState(wire);
    const out = after.exportBackgroundState() as { streams: Record<string, string> };
    expect(out.streams[String(ContainerId.Main)]).toContain('19 minutes');
  });

  it('refuses a corrupt snapshot instead of bricking the surface', () => {
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder());
    expect(hud.importBackgroundState({})).toBe(false);
    expect(hud.importBackgroundState({ surface: 'nonsense' })).toBe(false);
    expect(hud.importBackgroundState({ surface: { kind: 42 } })).toBe(false);
    // Garbage container keys are dropped rather than trusted.
    expect(hud.importBackgroundState({
      surface: { kind: 'default' },
      streams: { notANumber: 'x', 2: 'kept' },
    })).toBe(true);
    const out = hud.exportBackgroundState() as { streams: Record<string, string> };
    expect(out.streams).toEqual({ 2: 'kept' });
  });
});

describe('an answer is not an interruption', () => {
  const room = () => {
    const frames: HudVisualFrame[] = [];
    const visual = {
      paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as HudFramePainter;
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    return { hud, frames };
  };

  const session = {
    screen: 'brainstorm' as const,
    state: 'settled' as const, topic: 'cold-start latency',
    collaborators: ['Codex', 'Claude'], activity: 'JOAT > AGREED: cache it',
  };

  it('refuses unrelated chatter over a Brainstorm', async () => {
    // A queued reply once drained the instant a session settled and
    // painted "REPLY — Using the phone mic…" across the synthesis.
    const { hud } = room();
    expect(await hud.showBrainstorm(session as never)).toBe(true);
    expect(await hud.showAssistantOverlay('REPLY — using the phone mic'))
      .toBe(false);
  });

  it('shows the reply to something the wearer just asked there', async () => {
    // The rule was written as "never", which also silenced the answers TO
    // the wearer: "make that shorter" produced its result into nothing,
    // and the question asked before replacing a live session was never
    // seen — so they were waiting on a prompt that did not exist.
    const { hud } = room();
    expect(await hud.showBrainstorm(session as never)).toBe(true);
    expect(await hud.showAssistantOverlay('shorter version', false, true))
      .toBe(true);
    expect(hud.hasAssistantOverlay()).toBe(true);
  });
});


describe('Terminal opening paint ownership', () => {
  const model = { screen: 'agent_terminal', state: 'ready', topic: 'work', collaborators: [], activity: 'Ready' } as const;
  it('does not paint an opening retired before presentation', async () => {
    const paint = vi.fn(async () => true);
    const visual = { paint, paintExact: vi.fn(async (_value, current) => current()) };
    const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
    expect(await hud.showBrainstorm({ ...model, collaborators: [] }, undefined, () => false)).toBe(false);
    expect(paint).not.toHaveBeenCalled();
    expect(visual.paintExact).not.toHaveBeenCalled();
  });

  it.each([true, false])('a held opening settling %s cannot acknowledge or restore over Home', async (paintResult) => {
    // The Agent Terminal opens as Even's own two-text page (2026-09-13):
    // the held write is the terminal page rebuild under the transition
    // barrier, and Home waits behind that in-flight write before it owns
    // the glass. A retired opening may neither acknowledge nor paint its
    // old snapshot back over Home once the write settles.
    const frames: HudVisualFrame[] = [];
    const pages: RebuildPageContainer[] = [];
    const isTerminalPage = (page: RebuildPageContainer) =>
      page.textObject?.some((c) => c.containerName === 'terminal-body') ?? false;
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    let entered!: () => void;
    const began = new Promise<void>((resolve) => { entered = resolve; });
    let current = true;
    const client = {
      require: () => ({
        textContainerUpgrade: async () => true,
        rebuildPageContainer: async (page: RebuildPageContainer) => {
          pages.push(page);
          if (isTerminalPage(page)) { entered(); await held; return paintResult; }
          return true;
        },
      }),
    } as unknown as BridgeClient;
    const paint = async (frame: HudVisualFrame) => { frames.push(frame); return true; };
    const visual = {
      paint,
      paintExact: async (frame: HudVisualFrame, valid: () => boolean) => valid() && await paint(frame) && valid(),
      forgetPage: () => undefined,
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    };
    const hud = new HudPresenter(client, new HudPageBuilder(), visual);
    await hud.showText('Original feature', ContainerId.Main);
    const opening = hud.showBrainstorm({ ...model, collaborators: [] }, undefined, () => current);
    await began;
    current = false;
    const home = hud.showDefault();
    release();
    expect(await opening).toBe(false);
    expect(await home).toBe(true);
    expect(frames.map((frame) => frame.kind)).toEqual(['overview', 'overview']);
    expect(frames[frames.length - 1]).toMatchObject({ kind: 'overview', main: 'Original feature' });
    // One terminal page was ever written; the retired opening never
    // reinstalled it or its prior snapshot after the write settled.
    expect(pages.filter(isTerminalPage)).toHaveLength(1);
  });
});
