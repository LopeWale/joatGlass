import {
  RebuildPageContainer,
  TextContainerProperty,
  type TextContainerUpgrade,
} from '@evenrealities/even_hub_sdk';
import { getTextWidth } from '@evenrealities/pretext';
import { describe, expect, it, vi } from 'vitest';
import type { BridgeClient } from './BridgeClient';
import {
  NATIVE_TERMINAL_GEOMETRY,
  NativeTerminalContainer,
  NativeTerminalSurface,
  type NativeTerminalPage,
} from './NativeTerminalSurface';
import type { BrainstormHudModel, TerminalConversationMessage } from './types';

function terminal(overrides: Partial<BrainstormHudModel> = {}): BrainstormHudModel {
  return {
    screen: 'agent_terminal',
    state: 'ready',
    topic: 'OpenClaw · ready',
    collaborators: ['OpenClaw', 'Hermes'],
    activity: 'Ready',
    conversationId: 'openclaw-conversation',
    messages: [],
    noToolsChecked: true,
    ...overrides,
  };
}

function longTranscript(prefix: string): TerminalConversationMessage[] {
  return [{
    id: `${prefix}-answer`,
    role: 'assistant',
    text: Array.from({ length: 34 }, (_, index) => `${prefix} line ${index + 1}`).join('\n'),
  }];
}

function layout(page: Readonly<NativeTerminalPage>): RebuildPageContainer {
  return new RebuildPageContainer({
    containerTotalNum: 2,
    textObject: [
      new TextContainerProperty({
        containerID: NativeTerminalContainer.Head.id,
        containerName: NativeTerminalContainer.Head.name,
        xPosition: 0,
        yPosition: NATIVE_TERMINAL_GEOMETRY.headY,
        width: NATIVE_TERMINAL_GEOMETRY.width,
        height: NATIVE_TERMINAL_GEOMETRY.headHeight,
        paddingLength: NATIVE_TERMINAL_GEOMETRY.padding,
        borderWidth: 0,
        isEventCapture: 0,
        zOrderIndex: 1,
        textColor: 2,
        content: page.headText,
      }),
      new TextContainerProperty({
        containerID: NativeTerminalContainer.Body.id,
        containerName: NativeTerminalContainer.Body.name,
        xPosition: 0,
        yPosition: NATIVE_TERMINAL_GEOMETRY.bodyY,
        width: NATIVE_TERMINAL_GEOMETRY.width,
        height: NATIVE_TERMINAL_GEOMETRY.bodyHeight,
        paddingLength: NATIVE_TERMINAL_GEOMETRY.padding,
        borderWidth: 0,
        isEventCapture: 1,
        zOrderIndex: 2,
        textColor: 4,
        content: page.bodyText,
      }),
    ],
  });
}

interface RigOptions {
  upgrade?: (update: TextContainerUpgrade) => Promise<boolean>;
}

function rig(options: RigOptions = {}) {
  const pages: NativeTerminalPage[] = [];
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const imageWrites = vi.fn(async () => 'success');
  const textContainerUpgrade = vi.fn(async (update: TextContainerUpgrade) => {
    upgrades.push(update);
    return options.upgrade ? options.upgrade(update) : true;
  });
  const client = {
    require: () => ({ textContainerUpgrade, updateImageRawData: imageWrites }),
  } as unknown as Pick<BridgeClient, 'require'>;
  const replacePage = vi.fn(async (page: RebuildPageContainer) => {
    rebuilds.push(page);
    return true;
  });
  const surface = new NativeTerminalSurface(
    client,
    (page) => {
      pages.push({ ...page });
      return layout(page);
    },
    replacePage,
  );
  return { surface, pages, rebuilds, upgrades, imageWrites, textContainerUpgrade, replacePage };
}

function footer(page: NativeTerminalPage): string {
  const lines = page.bodyText.split('\n');
  return lines[lines.length - 1] ?? '';
}

function last<T>(values: readonly T[]): T | undefined {
  return values[values.length - 1];
}

describe('NativeTerminalSurface', () => {
  it('installs one two-text-container page, then uses one body upgrade per revision', async () => {
    const { surface, pages, rebuilds, upgrades, imageWrites } = rig();

    expect(await surface.present(terminal(), 'MIC OFF')).toBe(true);
    expect(rebuilds).toHaveLength(1);
    expect(upgrades).toHaveLength(0);
    expect(rebuilds[0]?.imageObject ?? []).toHaveLength(0);
    expect(rebuilds[0]?.textObject?.map((item) => item.containerName)).toEqual([
      'terminal-head', 'terminal-body',
    ]);
    expect(rebuilds[0]?.textObject?.filter((item) => item.isEventCapture === 1)
      .map((item) => item.containerName)).toEqual(['terminal-body']);
    expect(pages[0]?.bodyText.split('\n')).toHaveLength(NATIVE_TERMINAL_GEOMETRY.bodyLineCapacity);
    // The status line given to present() is the head; the body's state
    // row no longer repeats it.
    expect(pages[0]?.headText).toBe('MIC OFF');
    expect(pages[0]?.bodyText).toContain('OPENCLAW > READY');
    expect(pages[0]?.bodyText).not.toContain('OPENCLAW > READY · MIC OFF');
    expect(footer(pages[0]!)).toBe('tap: speak · 2x tap: back');
    expect(surface.scroll('up')).toBe(false);
    expect(surface.scroll('down')).toBe(false);

    expect(await surface.present(terminal({
      state: 'running',
      messages: [{ id: 'answer', role: 'assistant', text: 'Checking the live agent now.' }],
    }))).toBe(true);
    expect(rebuilds).toHaveLength(1);
    expect(upgrades).toHaveLength(1);
    expect(upgrades[0]).toMatchObject({
      containerID: NativeTerminalContainer.Body.id,
      containerName: NativeTerminalContainer.Body.name,
      contentOffset: 0,
    });
    expect(upgrades[0]?.content).toContain('OPENCLAW > WORKING');
    expect(upgrades[0]?.content).toContain('AGENT > Checking the live agent now.');
    // 2026-10-06: the running page names the gesture; "terminal cancel"
    // still works spoken.
    expect(upgrades[0]?.content).toContain('long tap: cancel · 2x tap: back');
    expect(imageWrites).not.toHaveBeenCalled();
  });

  it('uses the firmware metrics, reports only movable directions, and restores each conversation reading position', async () => {
    const { surface, pages, rebuilds, upgrades } = rig();
    const openClaw = terminal({ messages: [
      ...longTranscript('OpenClaw'),
      { id: 'wide', role: 'assistant', text: 'W'.repeat(260) },
    ] });

    expect(await surface.present(openClaw)).toBe(true);
    const tail = last(pages)!;
    expect(tail.older).toBeGreaterThan(0);
    expect(tail.newer).toBe(0);
    expect(footer(tail)).toContain('up: older');
    expect(footer(tail)).not.toContain('down: newer');
    expect(surface.scroll('down')).toBe(false);
    expect(surface.scroll('up')).toBe(true);
    await surface.whenIdle();
    const middle = last(upgrades)?.content ?? '';
    const middleLines = middle.split('\n');
    expect(last(middleLines)).toContain('up: older');
    expect(last(middleLines)).toContain('down: newer');

    while (surface.scroll('up')) await surface.whenIdle();
    const beginning = last(upgrades)?.content ?? '';
    const beginningLines = beginning.split('\n');
    expect(last(beginningLines)).not.toContain('up: older');
    expect(last(beginningLines)).toContain('down: newer');
    expect(surface.scroll('up')).toBe(false);

    expect(await surface.pauseForHandoff()).toBe(true);
    expect(surface.completeHandoff()).toBe(true);
    expect(await surface.present(terminal({
      topic: 'Hermes · ready',
      conversationId: 'hermes-conversation',
      messages: longTranscript('Hermes'),
    }))).toBe(true);
    expect(await surface.pauseForHandoff()).toBe(true);
    expect(surface.completeHandoff()).toBe(true);
    expect(await surface.present(openClaw)).toBe(true);
    expect(rebuilds).toHaveLength(3);
    const reopened = last(pages)!;
    expect(reopened.newer).toBeGreaterThan(0);
    expect(reopened.older).toBe(0);
    expect(footer(reopened)).toContain('down: newer');
    expect(footer(reopened)).not.toContain('up: older');

    for (const line of reopened.bodyText.split('\n')) {
      expect(getTextWidth(line)).toBeLessThanOrEqual(NATIVE_TERMINAL_GEOMETRY.bodyTextWidth);
    }
  });

  it('publishes a complete inline question in one accepted body transaction and rejects questions that cannot fit', async () => {
    let release: ((accepted: boolean) => void) | undefined;
    const accepted = new Promise<boolean>((resolve) => { release = resolve; });
    const { surface, upgrades, textContainerUpgrade } = rig({ upgrade: async () => accepted });
    expect(await surface.present(terminal())).toBe(true);

    const question = 'Which deployment should I inspect?\nInclude the affected service and region.';
    const publication = surface.showPrompt('prompt_1', question, () => true);
    await vi.waitFor(() => expect(textContainerUpgrade).toHaveBeenCalledTimes(1));
    expect(upgrades[0]?.containerName).toBe('terminal-body');
    expect(upgrades[0]?.content).toContain('QUESTION > Which deployment should I inspect?');
    expect(upgrades[0]?.content).toContain('Include the affected service and region.');
    expect(last(upgrades[0]?.content?.split('\n') ?? [])).toBe(
      'Listening · speak your answer · 2x tap: cancel',
    );
    expect(surface.scroll('up')).toBe(false);

    release?.(true);
    expect(await publication).toBe(true);
    expect(await surface.dismissPrompt('some-other-prompt')).toBe(false);
    expect(textContainerUpgrade).toHaveBeenCalledTimes(1);

    textContainerUpgrade.mockImplementation(async (update) => {
      upgrades.push(update);
      return true;
    });
    expect(await surface.dismissPrompt('prompt_1')).toBe(true);
    expect(textContainerUpgrade).toHaveBeenCalledTimes(2);
    expect(last(upgrades)?.content).not.toContain('QUESTION >');

    const before = textContainerUpgrade.mock.calls.length;
    expect(await surface.showPrompt(
      'prompt_2',
      Array.from({ length: 7 }, (_, index) => `question line ${index}`).join('\n'),
    )).toBe(false);
    expect(textContainerUpgrade).toHaveBeenCalledTimes(before);
  });

  it('coalesces queued stream revisions while keeping native writes strictly serial', async () => {
    let firstRelease: ((accepted: boolean) => void) | undefined;
    const first = new Promise<boolean>((resolve) => { firstRelease = resolve; });
    let calls = 0;
    let inFlight = 0;
    let maximumInFlight = 0;
    const { surface, upgrades, textContainerUpgrade } = rig({
      upgrade: async () => {
        calls += 1;
        inFlight += 1;
        maximumInFlight = Math.max(maximumInFlight, inFlight);
        const result = calls === 1 ? await first : true;
        inFlight -= 1;
        return result;
      },
    });
    expect(await surface.present(terminal())).toBe(true);

    const revision = (id: string, text: string) => terminal({
      state: 'running', messages: [{ id, role: 'assistant', text }],
    });
    const one = surface.present(revision('one', 'first streamed chunk'));
    await vi.waitFor(() => expect(textContainerUpgrade).toHaveBeenCalledTimes(1));
    const two = surface.present(revision('two', 'intermediate streamed chunk'));
    const three = surface.present(revision('three', 'final streamed answer'));
    firstRelease?.(true);

    expect(await one).toBe(false);
    expect(await two).toBe(false);
    expect(await three).toBe(true);
    await surface.whenIdle();
    expect(maximumInFlight).toBe(1);
    expect(textContainerUpgrade).toHaveBeenCalledTimes(2);
    expect(upgrades[0]?.content).toContain('first streamed chunk');
    expect(upgrades[1]?.content).toContain('final streamed answer');
    expect(upgrades[1]?.content).not.toContain('intermediate streamed chunk');
    expect(upgrades[1]?.contentLength).toBeGreaterThanOrEqual(upgrades[0]?.content?.length ?? 0);
  });

  it('quiesces for page handoffs, rebuilds after a failed handoff, and can retire cleanly', async () => {
    const { surface, rebuilds, upgrades } = rig();
    expect(await surface.present(terminal({ messages: longTranscript('OpenClaw') }))).toBe(true);
    expect(surface.scroll('up')).toBe(true);
    await surface.whenIdle();
    const writesBeforePause = upgrades.length;

    expect(await surface.pauseForHandoff()).toBe(true);
    expect(surface.isActive()).toBe(true);
    expect(surface.scroll('up')).toBe(false);
    expect(await surface.showPrompt('paused_prompt', 'This must not appear.')).toBe(false);
    expect(await surface.present(terminal({ state: 'running' }))).toBe(false);
    expect(upgrades).toHaveLength(writesBeforePause);

    expect(await surface.resume()).toBe(true);
    expect(rebuilds).toHaveLength(2);
    expect(surface.isActive()).toBe(true);
    expect(await surface.pauseForHandoff()).toBe(true);
    expect(surface.completeHandoff()).toBe(true);
    expect(surface.isActive()).toBe(false);
    expect(surface.completeHandoff()).toBe(false);

    expect(await surface.present(terminal({ messages: longTranscript('OpenClaw') }))).toBe(true);
    expect(surface.retire()).toBe(true);
    expect(surface.retire()).toBe(false);
    await surface.whenIdle();
    expect(surface.isActive()).toBe(false);
  });

  it('recovers a missing native body by reinstalling the same two-text page once', async () => {
    let attempts = 0;
    const { surface, rebuilds, textContainerUpgrade, imageWrites } = rig({
      upgrade: async () => {
        attempts += 1;
        return attempts > 1;
      },
    });
    expect(await surface.present(terminal())).toBe(true);
    expect(await surface.present(terminal({
      state: 'running',
      messages: [{ id: 'answer', role: 'assistant', text: 'Checking the provider.' }],
    }))).toBe(true);
    expect(textContainerUpgrade).toHaveBeenCalledTimes(1);
    expect(rebuilds).toHaveLength(2);
    expect(rebuilds[1]?.textObject?.map((item) => item.containerName)).toEqual([
      'terminal-head', 'terminal-body',
    ]);
    expect(rebuilds[1]?.imageObject ?? []).toHaveLength(0);
    expect(imageWrites).not.toHaveBeenCalled();
  });
});


/** Brainstorm (Codex proposes, Claude critiques) shares the native page.
 * The head names the feature, the status row carries the state and the
 * live speaker, the transcript is the Mac's activity text, and the footer
 * names the gestures BrainstormController accepts in that state. */
describe('Brainstorm on the native page', () => {
  function brainstorm(overrides: Partial<BrainstormHudModel> = {}): BrainstormHudModel {
    return {
      screen: 'brainstorm',
      state: 'ready',
      topic: 'Ready for a question',
      collaborators: ['Codex', 'Claude'],
      activity: 'Tap to choose a question, then speak.',
      ...overrides,
    };
  }

  it('draws the ready frame as the two-text page with no image or list objects', async () => {
    const r = rig();
    expect(await r.surface.present(brainstorm(), '12:34 JOAT mic off bat 80%')).toBe(true);
    expect(r.rebuilds).toHaveLength(1);
    expect(r.imageWrites).not.toHaveBeenCalled();
    const page = r.pages[0]!;
    expect(page.headText).toBe('12:34 JOAT mic off bat 80%');
    const lines = page.bodyText.split('\n');
    expect(lines[0]).toBe('READY · Ready for a question');
    expect(lines).toContain('Tap to choose a question, then speak.');
    expect(footer(page)).toBe('tap: speak · 2x tap: menu');
  });

  it('streams a running session through body upgrades and names the live speaker', async () => {
    const r = rig();
    await r.surface.present(brainstorm());
    const running = brainstorm({
      state: 'running', topic: 'Should the glasses cache the picker?',
      activeSpeaker: 'Codex · round 1/3 · 2 calls',
      activity: 'YOU > Should the glasses cache the picker?\nCODEX > A cache saves a round trip but',
    });
    expect(await r.surface.present(running)).toBe(true);
    expect(await r.surface.present({ ...running, activity: `${running.activity} risks stale rows.` })).toBe(true);
    expect(r.rebuilds).toHaveLength(1);
    expect(r.upgrades.length).toBeGreaterThanOrEqual(2);
    // Revisions are body upgrades; the installed page is not rebuilt.
    const lines = (last(r.upgrades)?.content ?? '').split('\n');
    expect(lines[0]).toBe('WORKING · Codex · round 1/3 · 2 calls');
    expect(lines.some((line) => line.startsWith('CODEX > '))).toBe(true);
    expect(lines[lines.length - 1]).toContain('2x tap: cancel');
  });

  it('shows the listening and unavailable states with their own footers', async () => {
    const r = rig();
    expect(await r.surface.present(brainstorm({ state: 'topic_capture', topic: 'Speak your question' }))).toBe(true);
    expect(last(r.pages)!.bodyText.split('\n')[0]).toBe('LISTENING · Speak your question');
    expect(footer(last(r.pages)!)).toBe('speak your question · 2x tap: cancel');
    expect(await r.surface.present(brainstorm({
      state: 'unavailable', topic: 'Brainstorm', collaborators: ['Claude'],
      activity: 'codex CLI missing on the Mac — install to enable.',
    }))).toBe(true);
    const body = last(r.upgrades)?.content ?? '';
    expect(body.split('\n')[0]).toBe('UNAVAILABLE · Brainstorm');
    expect(body).toContain('codex CLI missing on the Mac');
  });
});
