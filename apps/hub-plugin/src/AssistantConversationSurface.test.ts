import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { HudPageBuilder } from './HudPageBuilder';
import {
  AssistantConversationSurface,
  conversationLines,
  conversationViewport,
  type AssistantConversationSnapshot,
} from './AssistantConversationSurface';

function snapshot(
  revision: number,
  turns: AssistantConversationSnapshot['turns'],
  state: AssistantConversationSnapshot['state'] = 'ready',
): AssistantConversationSnapshot {
  return {
    conversationId: 'conversation-1',
    revision,
    state,
    turns,
    continueAfterMs: 30_000,
    earlierTurnsOmitted: false,
  };
}

function fakeClient() {
  const rebuildPageContainer = vi.fn(async (_page: RebuildPageContainer) => true);
  const textContainerUpgrade = vi.fn(async (_update: TextContainerUpgrade) => true);
  return {
    client: {
      require: () => ({ rebuildPageContainer, textContainerUpgrade }),
    },
    rebuildPageContainer,
    textContainerUpgrade,
  };
}

describe('native Even-style assistant conversation model', () => {
  it('keeps the user prompt and assistant reply as ordered turns in one transcript', () => {
    const model = snapshot(2, [
      { role: 'user', text: 'What is the capital of France?' },
      { role: 'assistant', text: 'Paris.' },
      { role: 'user', text: 'Tell me a quick joke.' },
      { role: 'assistant', text: 'Why did the byte cross the bus? To get to the other side.' },
    ]);

    const lines = conversationLines(model);
    expect(lines.filter((line) => line.role === 'user').map((line) => line.text).join(' '))
      .toContain('What is the capital of France?');
    expect(lines.filter((line) => line.role === 'assistant').map((line) => line.text).join(' '))
      .toContain('Paris.');
    expect(lines.findIndex((line) => line.text.includes('capital')))
      .toBeLessThan(lines.findIndex((line) => line.text.includes('Paris')));
    expect(lines.findIndex((line) => line.text.includes('quick joke')))
      .toBeLessThan(lines.findIndex((line) => line.text.includes('byte')));
  });

  it('uses dim and bright layers for wearer and assistant without losing either layer', () => {
    const view = conversationViewport(snapshot(1, [
      { role: 'user', text: 'What is the weather?' },
      { role: 'assistant', text: 'It is warm and partly cloudy.' },
    ]));

    const dim = view.dimText.split('\n');
    const bright = view.brightText.split('\n');
    expect(dim).toHaveLength(bright.length);
    expect(dim.some((line) => line.includes('What is the weather?'))).toBe(true);
    expect(bright.some((line) => line.includes('warm and partly cloudy'))).toBe(true);
    expect(dim.some((line, index) => line.trim() && bright[index]?.trim())).toBe(false);
  });

  it('grows only to the optical cap, then scrolls off the top with no scrollbar', () => {
    // Even AI draws no scrollbar (product-page demo): overflow leaves
    // through the top of the card, and scrollback pages the window.
    const long = snapshot(3, [
      { role: 'user', text: 'Explain why the daytime sky looks blue.' },
      { role: 'assistant', text: Array.from({ length: 90 }, (_, i) => `word${i}`).join(' ') },
    ]);
    const tail = conversationViewport(long);
    expect(tail.lineCapacity).toBe(7);
    expect(tail.totalLines).toBeGreaterThan(tail.lineCapacity);

    const top = conversationViewport(long, Number.MAX_SAFE_INTEGER);
    expect(top.scrollFromTail).toBe(top.maximumScrollFromTail);
    expect(top.visibleLines[0]?.text).toContain('Explain why');
    expect(top.brightText).not.toEqual(tail.brightText);
  });

  it('keeps the exact long-answer tail instead of clipping or reflowing the ending', () => {
    const view = conversationViewport(snapshot(11, [
      { role: 'user', text: 'what is the capital of France?' },
      { role: 'assistant', text: 'Paris.' },
      { role: 'user', text: 'tell me a developer joke' },
      {
        role: 'assistant',
        text: 'Why did the developer bring glasses? Because every bug deserved a closer look.',
      },
      {
        role: 'user',
        text: 'in four full sentences, explain why the sky is blue and sunsets are red',
      },
      {
        role: 'assistant',
        text: "Sunlight contains many colors, and Earth's atmosphere scatters shorter blue wavelengths more strongly than longer red wavelengths, making the daytime sky look blue. At sunset, sunlight travels through more atmosphere, so most blues are scattered away before the light reaches you. The remaining direct light is richer in reds and oranges, which paints the Sun and nearby sky in warm colors. Dust, smoke, clouds, and pollution can intensify or mute those sunset colors.",
      },
    ]));

    expect(view.visibleLines).toEqual([
      { role: 'assistant', text: 'daytime sky look blue. At sunset, sunlight travels' },
      { role: 'assistant', text: 'through more atmosphere, so most blues are' },
      { role: 'assistant', text: 'scattered away before the light reaches you. The' },
      { role: 'assistant', text: 'remaining direct light is richer in reds and oranges,' },
      { role: 'assistant', text: 'which paints the Sun and nearby sky in warm colors.' },
      { role: 'assistant', text: 'Dust, smoke, clouds, and pollution can intensify or' },
      { role: 'assistant', text: 'mute those sunset colors.' },
    ]);
  });

  it('marks omitted history instead of silently pretending the viewport is complete', () => {
    const view = conversationViewport({
      ...snapshot(4, [{ role: 'assistant', text: 'Newest retained answer.' }]),
      earlierTurnsOmitted: true,
    }, Number.MAX_SAFE_INTEGER);
    expect(view.visibleLines.some((line) => line.text === 'Earlier conversation…')).toBe(true);
  });
});

describe('AssistantConversationSurface delivery', () => {
  it('gives streamed answer pixels priority over decorative activity ticks', async () => {
    vi.useFakeTimers();
    const { client } = fakeClient();
    const painted: Array<{ state: string; indicatorFrame: number }> = [];
    const surface = new AssistantConversationSurface(
      client as never,
      new HudPageBuilder(),
      async () => true,
      {
        indicatorIntervalMs: 450,
        continuationPromptEnabled: false,
        paintView: async (view) => {
          painted.push({ state: view.state, indicatorFrame: view.indicatorFrame });
          return true;
        },
      },
    );
    try {
      await surface.present(snapshot(1, [
        { role: 'user', text: 'Tell me something.' },
      ], 'thinking'));
      await vi.advanceTimersByTimeAsync(450);
      await surface.whenIdle();
      expect(painted[painted.length - 1]).toEqual({
        state: 'thinking', indicatorFrame: 1,
      });

      await surface.present(snapshot(2, [
        { role: 'user', text: 'Tell me something.' },
        { role: 'assistant', text: 'The answer has started' },
      ], 'answering'));
      await surface.whenIdle();
      const afterAnswerPixels = painted.length;
      await vi.advanceTimersByTimeAsync(1_999);
      await surface.whenIdle();
      expect(painted).toHaveLength(afterAnswerPixels);
      await vi.advanceTimersByTimeAsync(1);
      await surface.whenIdle();
      expect(painted).toHaveLength(afterAnswerPixels + 1);
    } finally {
      surface.retireForHandoff();
      vi.useRealTimers();
    }
  });

  it('grows the card at stage boundaries and streams within a stage, never image tiles', async () => {
    // Even AI's card hugs its content and grows upward as the exchange
    // grows (product-page demo). A stage crossing is exactly one rebuild;
    // a newer revision INSIDE a stage is in-place text upgrades only.
    const { client, rebuildPageContainer, textContainerUpgrade } = fakeClient();
    const restore = vi.fn(async () => true);
    const surface = new AssistantConversationSurface(
      client as never,
      new HudPageBuilder(),
      restore,
      { indicatorIntervalMs: 0, continuationPromptEnabled: false },
    );

    await expect(surface.present(snapshot(1, [
      { role: 'user', text: "What is France's\ncapital?" },
    ], 'thinking'))).resolves.toBe(true);
    expect(rebuildPageContainer).toHaveBeenCalledTimes(1); // 2 lines: stage 2

    await surface.present(snapshot(2, [
      { role: 'user', text: "What is France's\ncapital?" },
      { role: 'assistant', text: 'Paris.' },
    ], 'answering'));
    await surface.whenIdle();
    expect(rebuildPageContainer).toHaveBeenCalledTimes(2); // 3 lines: stage 3

    const upgradesBefore = textContainerUpgrade.mock.calls.length;
    await surface.present(snapshot(3, [
      { role: 'user', text: "What is France's\ncapital?" },
      { role: 'assistant', text: 'Paris, of course.' },
    ], 'answering'));
    await surface.whenIdle();
    expect(rebuildPageContainer).toHaveBeenCalledTimes(2); // same stage: no rebuild
    expect(textContainerUpgrade.mock.calls.length).toBeGreaterThan(upgradesBefore);

    const everyContent = [
      ...rebuildPageContainer.mock.calls.flatMap(([page]) => page.textObject ?? []),
      ...textContainerUpgrade.mock.calls.map(([update]) => update),
    ].map((entry) => entry.content ?? '').join('\n');
    expect(everyContent).toContain("What is France's");
    expect(everyContent).toContain('capital?');
    expect(everyContent).toContain('Paris, of course.');
  });

  it('coalesces a fast final answer behind a slow native update instead of queuing every preview', async () => {
    let releaseFirstUpdate!: (value: boolean) => void;
    const firstUpdate = new Promise<boolean>((resolve) => { releaseFirstUpdate = resolve; });
    const rebuildPageContainer = vi.fn(async (_page: RebuildPageContainer) => true);
    const textContainerUpgrade = vi.fn<(update: TextContainerUpgrade) => Promise<boolean>>()
      .mockImplementationOnce(() => firstUpdate)
      .mockResolvedValue(true);
    const client = { require: () => ({ rebuildPageContainer, textContainerUpgrade }) };
    const surface = new AssistantConversationSurface(
      client as never,
      new HudPageBuilder(),
      async () => true,
      { indicatorIntervalMs: 0, continuationPromptEnabled: false },
    );

    await surface.present(snapshot(1, [{ role: 'user', text: 'Tell me a joke.\nKeep it short.' }], 'thinking'));
    await surface.present(snapshot(2, [
      { role: 'user', text: 'Tell me a joke.\nKeep it short.' },
      { role: 'assistant', text: 'Why did the byte cross the bus?' },
    ], 'answering'));
    await surface.present(snapshot(3, [
      { role: 'user', text: 'Tell me a joke.\nKeep it short.' },
      { role: 'assistant', text: 'Why did the byte cross the bus? To get to the other side.' },
    ], 'ready'));

    releaseFirstUpdate(true);
    await surface.whenIdle();
    const content = [
      ...rebuildPageContainer.mock.calls.flatMap(([page]) => page.textObject ?? []),
      ...textContainerUpgrade.mock.calls.map(([update]) => update),
    ]
      .map((entry) => entry.content ?? '')
      .join('\n')
      .replace(/\s+/gu, ' ');
    expect(content).toContain('To get to the other side.');
    expect(surface.currentRevision()).toBe(3);
    expect(textContainerUpgrade.mock.calls.length).toBeLessThan(8);
  });

  it('commits a scroll position only after its native frame succeeds', async () => {
    const { client, textContainerUpgrade } = fakeClient();
    const surface = new AssistantConversationSurface(
      client as never,
      new HudPageBuilder(),
      async () => true,
      { indicatorIntervalMs: 0, continuationPromptEnabled: false },
    );
    const long = snapshot(1, [
      { role: 'user', text: 'Explain the sky.' },
      { role: 'assistant', text: Array.from({ length: 90 }, (_, i) => `word${i}`).join(' ') },
    ]);
    await surface.present(long);
    textContainerUpgrade.mockResolvedValueOnce(false);
    const rebuildPageContainer = client.require().rebuildPageContainer as ReturnType<typeof vi.fn>;
    rebuildPageContainer.mockResolvedValueOnce(false);

    expect(surface.scroll('up')).toBe(true);
    await surface.whenIdle();
    expect(surface.scrollFromTail()).toBe(0);
  });

  it('asks before leaving, continues in place, and retires stale late revisions after exit', async () => {
    const { client } = fakeClient();
    const restore = vi.fn(async () => true);
    const surface = new AssistantConversationSurface(
      client as never,
      new HudPageBuilder(),
      restore,
      { indicatorIntervalMs: 0, continuationPromptEnabled: false },
    );
    await surface.present(snapshot(1, [
      { role: 'user', text: 'Are you there?' },
      { role: 'assistant', text: 'Yes.' },
    ]));

    await expect(surface.requestExit()).resolves.toBe('prompted');
    expect(surface.isContinuePromptVisible()).toBe(true);
    await expect(surface.continueConversation()).resolves.toBe(true);
    expect(surface.isActive()).toBe(true);
    await expect(surface.requestExit()).resolves.toBe('prompted');
    await expect(surface.requestExit()).resolves.toBe('exited');
    expect(restore).toHaveBeenCalledTimes(1);
    expect(surface.isActive()).toBe(false);

    await expect(surface.present(snapshot(99, [
      { role: 'assistant', text: 'A stale answer that must not resurrect.' },
    ]))).resolves.toBe(false);
    expect(surface.isActive()).toBe(false);
  });

  it('overwrites the complete prior native value when the footer shrinks', async () => {
    const { client, textContainerUpgrade } = fakeClient();
    const surface = new AssistantConversationSurface(
      client as never,
      new HudPageBuilder(),
      async () => true,
      { indicatorIntervalMs: 0, continuationPromptEnabled: false },
    );
    await surface.present(snapshot(1, [
      { role: 'user', text: 'Still there?' },
      { role: 'assistant', text: 'Yes.' },
    ]));

    await expect(surface.requestExit()).resolves.toBe('prompted');
    await expect(surface.continueConversation()).resolves.toBe(true);
    await surface.whenIdle();

    const footerUpdates = textContainerUpgrade.mock.calls
      .map(([update]) => update)
      .filter((update) => update.containerName === 'generation');
    const prompt = footerUpdates.find(
      (update) => update.content?.trim() === 'Continue? tap: stay · double-click: back',
    );
    const ready = footerUpdates[footerUpdates.length - 1];
    expect(prompt).toBeDefined();
    expect(ready?.content?.trim()).toBe('■');
    expect(ready?.contentOffset).toBe(0);
    expect(ready?.contentLength).toBeGreaterThanOrEqual(prompt?.content?.length ?? 0);
  });

  it('does not report a continue or exit choice until its native frame succeeds', async () => {
    const { client, textContainerUpgrade } = fakeClient();
    const surface = new AssistantConversationSurface(
      client as never,
      new HudPageBuilder(),
      async () => true,
      { indicatorIntervalMs: 0, continuationPromptEnabled: false },
    );
    await surface.present(snapshot(3, [
      { role: 'user', text: 'Can I leave?' },
      { role: 'assistant', text: 'Yes.' },
    ]));
    textContainerUpgrade.mockResolvedValueOnce(false);
    (client.require().rebuildPageContainer as ReturnType<typeof vi.fn>).mockResolvedValueOnce(false);

    await expect(surface.requestExit()).resolves.toBe('failed');
    expect(surface.isContinuePromptVisible()).toBe(true);
    await expect(surface.dismiss('conversation-1', 4)).resolves.toBe(false);
    expect(surface.isActive()).toBe(true);
  });

  it('drains an in-flight native write before handoff and accepts the same conversation afterward', async () => {
    let releaseUpdate!: (value: boolean) => void;
    const heldUpdate = new Promise<boolean>((resolve) => { releaseUpdate = resolve; });
    const rebuildPageContainer = vi.fn(async (_page: RebuildPageContainer) => true);
    const textContainerUpgrade = vi.fn<(update: TextContainerUpgrade) => Promise<boolean>>()
      .mockImplementationOnce(() => heldUpdate)
      .mockResolvedValue(true);
    const client = { require: () => ({ rebuildPageContainer, textContainerUpgrade }) };
    const surface = new AssistantConversationSurface(
      client as never,
      new HudPageBuilder(),
      async () => true,
      { indicatorIntervalMs: 0, continuationPromptEnabled: false },
    );
    await surface.present(snapshot(1, [{
      role: 'user', text: 'First prompt\nwith a second line',
    }], 'thinking'));
    await surface.present(snapshot(2, [
      { role: 'user', text: 'First prompt\nwith a second line' },
      { role: 'assistant', text: 'A partial answer' },
    ], 'answering'));

    let paused = false;
    const pause = surface.pauseForHandoff().then((value) => { paused = value; });
    await Promise.resolve();
    expect(paused).toBe(false);
    releaseUpdate(true);
    await pause;
    expect(paused).toBe(true);
    expect(surface.completeHandoff()).toBe(true);
    expect(surface.isActive()).toBe(false);

    await expect(surface.present(snapshot(3, [
      { role: 'user', text: 'First prompt\nwith a second line' },
      { role: 'assistant', text: 'The completed answer' },
    ], 'ready'))).resolves.toBe(true);
    // Three rebuilds: the first card (stage 2), the answer crossing into
    // stage 3, and the restore after handoff.
    expect(rebuildPageContainer).toHaveBeenCalledTimes(3);
    expect(surface.currentIdentity()).toEqual({ conversationId: 'conversation-1', revision: 3 });
  });
});
