import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { HudPresenter } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import { ContainerId } from './types';

/**
 * Streamed paints must be SELF-CONTAINED full frames, throttled to BLE
 * reality. Per-token offset splicing rendered as flashing fragments on real
 * hardware ("cutting out, one line") — these tests pin the replacement
 * semantics so it cannot come back.
 */

const paints: TextContainerUpgrade[] = [];

function fakeClient(): BridgeClient {
  return {
    require: () => ({
      textContainerUpgrade: async (u: TextContainerUpgrade) => {
        paints.push(u);
        return true;
      },
      rebuildPageContainer: async () => true,
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
