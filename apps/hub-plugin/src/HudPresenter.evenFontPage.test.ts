import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import type { HudVisualFrame } from './HudVisualSurface';

setNativeOverviewForTest(false);

/**
 * A reader set in Even's font at 66% (EvenFont.ts) is an image page: the
 * four tiles over a full-screen input layer, painted by the visual lane.
 * It pages locally by repainting, hands the edge back to the Mac exactly
 * as the firmware text page does, and survives a trip to the background.
 */
function rig(canPaintReader = true) {
  const operations: string[] = [];
  const rebuilds: RebuildPageContainer[] = [];
  const client = {
    require: () => ({
      rebuildPageContainer: vi.fn(async (page: RebuildPageContainer) => {
        operations.push(`rebuild:${page.imageObject?.length ?? 0}:${page.textObject?.length ?? 0}`);
        rebuilds.push(page);
        return true;
      }),
      textContainerUpgrade: vi.fn(async () => { operations.push('upgrade'); return true; }),
    }),
  } as unknown as BridgeClient;
  const frames: HudVisualFrame[] = [];
  const visual = {
    paint: vi.fn(async (frame: HudVisualFrame) => { operations.push(`visual:${frame.kind}`); frames.push(frame); return true; }),
    paintExact: vi.fn(async (frame: HudVisualFrame, current: () => boolean) => {
      if (!current()) return false;
      operations.push(`exact:${frame.kind}`);
      frames.push(frame);
      return true;
    }),
    forgetPage: vi.fn(),
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
    canPaintReader: () => canPaintReader,
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  return { hud, operations, rebuilds, frames };
}

/** Enough of a thread for two Even-font pages. */
const THREAD = [
  '1/1 · Adele',
  ...Array.from({ length: 14 }, (_, index) => `${index % 2 ? 'You' : 'Adele'}: Message ${index + 1} about the review and the mockups.\n`),
];
const OPTIONS = {
  title: 'MESSAGES · CONVERSATION', hint: 'down more · tap: reply · left x2: back',
  selectable: true, nativeTextPage: true,
};

function lastReader(frames: HudVisualFrame[]): Extract<HudVisualFrame, { kind: 'reader' }> {
  const frame = [...frames].reverse().find((candidate) => candidate.kind === 'reader');
  if (!frame || frame.kind !== 'reader') throw new Error('no reader frame');
  return frame;
}

describe('HudPresenter Even-font reader page', () => {
  it('opens the thread as the four-tile image page, set in Even font', async () => {
    const r = rig();
    // From the Messages list, a firmware list, as the wearer gets there.
    await r.hud.showList(['[2m] Adele · Can you send…'], { title: 'MESSAGES', hint: 'tap: open', selectable: true });
    expect(await r.hud.showList(THREAD, OPTIONS)).toBe(true);
    const page = r.rebuilds[r.rebuilds.length - 1];
    expect(page?.imageObject).toHaveLength(4);
    expect(page?.textObject?.[0]?.isEventCapture).toBe(1);
    const frame = lastReader(r.frames);
    expect(frame.title).toBe('MESSAGES · CONVERSATION');
    expect(frame.lines[0]?.text).toBe('1/1 · Adele');
    expect(frame.lines.find((line) => line.text.startsWith('Adele:'))?.speaker).toBe('Adele:'.length);
    expect(frame.hint.startsWith('1/2 · ')).toBe(true);
    expect(r.operations).not.toContain('upgrade');
  });

  it('pages by repainting, and hands the edge back to the Mac', async () => {
    const r = rig();
    await r.hud.showList(THREAD, OPTIONS);
    const rebuilds = r.rebuilds.length;
    expect(r.hud.isTextPageAtEnd()).toBe(false);
    expect(r.hud.scrollTextPage('up')).toBe(false);
    const before = r.frames.length;
    expect(r.hud.scrollTextPage('down')).toBe(true);
    await vi.waitFor(() => expect(r.frames.length).toBeGreaterThan(before));
    const second = lastReader(r.frames);
    expect(second.hint.startsWith('2/2 · ')).toBe(true);
    expect(second.lines[0]?.text).not.toBe('1/1 · Adele');
    expect(r.hud.isTextPageAtEnd()).toBe(true);
    // Past the last local page the menu asks the Mac for older messages.
    expect(r.hud.scrollTextPage('down')).toBe(false);
    // Paging never rebuilds the page: only changed tiles are painted.
    expect(r.rebuilds).toHaveLength(rebuilds);
  });

  it('keeps the firmware text page where the painter cannot draw readers', async () => {
    const r = rig(false);
    await r.hud.showList(THREAD, OPTIONS);
    const page = r.rebuilds[r.rebuilds.length - 1];
    expect(page?.imageObject ?? []).toHaveLength(0);
    expect(r.frames.some((frame) => frame.kind === 'reader')).toBe(false);
  });

  it('keeps the reader in Even font above JOAT\'s box, and whole again after it', async () => {
    const r = rig();
    await r.hud.showList(THREAD, OPTIONS);
    expect(await r.hud.showPromptQuestion('p1', 'What do you want to say back to Adele?', () => true, 'messages')).toBe(true);
    const page = r.rebuilds[r.rebuilds.length - 1];
    // The thread's four tiles stay beneath the box; the box takes input.
    expect(page?.imageObject).toHaveLength(4);
    expect(page?.textObject?.filter((text) => text.isEventCapture === 1)).toHaveLength(1);
    await vi.waitFor(() => expect(lastReader(r.frames).clipBottom).toBeDefined());
    const top = page?.textObject?.find((text) => text.containerName === 'conversation')?.yPosition ?? 0;
    expect(lastReader(r.frames).clipBottom).toBeLessThan(top);

    await r.hud.dismissPromptQuestion('p1');
    await vi.waitFor(() => expect(lastReader(r.frames).clipBottom).toBeUndefined());
  });

  it('resumes the same page after the background migration', async () => {
    const r = rig();
    await r.hud.showList(THREAD, OPTIONS);
    r.hud.scrollTextPage('down');
    await vi.waitFor(() => expect(lastReader(r.frames).hint.startsWith('2/2')).toBe(true));
    const saved = JSON.parse(JSON.stringify(r.hud.exportBackgroundState())) as Record<string, unknown>;

    const restored = rig();
    expect(restored.hud.importBackgroundState(saved)).toBe(true);
    expect(await restored.hud.repaintRestoredSurface()).toBe(true);
    expect(lastReader(restored.frames).hint.startsWith('2/2 · ')).toBe(true);
  });
});
