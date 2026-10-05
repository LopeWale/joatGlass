import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter, paginateAnswer } from './HudPresenter';
import { ContainerId } from './types';
import type { HudVisualFrame } from './HudVisualSurface';

/**
 * A JOAT answer over the visual HUD (a command result, an error, a
 * capability answer) is Even's own text page: one rebuild in, in-place
 * upgrades to page or stream, four image containers back on dismissal.
 * Receipts keep the image strip. These cases pin that split.
 */
function rig() {
  const operations: string[] = [];
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const client = {
    require: () => ({
      rebuildPageContainer: vi.fn(async (page: RebuildPageContainer) => {
        operations.push(`rebuild:${page.imageObject?.length ?? 0}:${page.textObject?.length ?? 0}:${page.listObject?.length ?? 0}`);
        rebuilds.push(page);
        return true;
      }),
      textContainerUpgrade: vi.fn(async (update: TextContainerUpgrade) => {
        operations.push(`upgrade:${update.containerName ?? update.containerID}`);
        upgrades.push(update);
        return true;
      }),
    }),
  } as unknown as BridgeClient;
  const frames: HudVisualFrame[] = [];
  const visual = {
    paint: vi.fn(async (frame: HudVisualFrame) => {
      operations.push(`visual:${frame.kind}`);
      frames.push(frame);
      return true;
    }),
    forgetPage: vi.fn(() => operations.push('forget')),
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  return { hud, operations, rebuilds, upgrades, frames, visual };
}

function text(page: RebuildPageContainer | undefined, name: string): string | undefined {
  return page?.textObject?.find((container) => container.containerName === name)?.content;
}

const longAnswer = Array.from({ length: 12 }, (_, index) => (
  `Line ${index + 1} of a long answer that wraps past seven firmware rows.`
)).join('\n');

describe('HudPresenter native answer page over the visual HUD', () => {
  it('shows an answer as one text-only page carrying the status line, with no image tiles', async () => {
    const r = rig();
    await r.hud.showText('12:34 JOAT listening bat 80%', ContainerId.StatusBar);
    await r.hud.showText('home card', ContainerId.Main);
    const framesBefore = r.frames.length;

    expect(await r.hud.showAssistantOverlay('Submitted to Messages for Adaeze; delivery unverified.')).toBe(true);

    const page = r.rebuilds[r.rebuilds.length - 1];
    expect(page?.imageObject ?? []).toHaveLength(0);
    expect(page?.textObject?.map((c) => c.containerName)).toEqual(['status', 'main', 'hint']);
    expect(text(page, 'main')).toBe('Submitted to Messages for Adaeze; delivery unverified.');
    expect(text(page, 'status')).toBe('12:34 JOAT listening bat 80%');
    expect(text(page, 'hint')).toBe('tap: dismiss');
    expect(r.frames).toHaveLength(framesBefore);
    expect(r.hud.hasAssistantOverlay()).toBe(true);
  });

  it('pages a long answer in place and returns the image page on dismissal', async () => {
    const r = rig();
    await r.hud.showText('home card', ContainerId.Main);
    expect(await r.hud.showAssistantOverlay(longAnswer)).toBe(true);
    const pages = paginateAnswer(longAnswer);
    expect(pages.length).toBeGreaterThan(1);
    const first = r.rebuilds[r.rebuilds.length - 1];
    expect(text(first, 'main')).toBe(pages[0]);
    expect(text(first, 'hint')).toBe(`1/${pages.length} · swipe: page · tap: dismiss`);
    const rebuildsAfterOpen = r.rebuilds.length;

    expect(r.hud.scrollAssistantOverlay('down')).toBe(true);
    await vi.waitFor(() => expect(r.upgrades.some((u) => u.containerName === 'main' && u.content === pages[1])).toBe(true));
    expect(r.upgrades.some((u) => u.containerName === 'hint' && u.content === `2/${pages.length} · swipe: page · tap: dismiss`)).toBe(true);
    expect(r.rebuilds).toHaveLength(rebuildsAfterOpen);

    const before = r.operations.length;
    expect(await r.hud.dismissAssistantOverlay()).toBe(true);
    expect(r.operations.slice(before)).toEqual(['rebuild:4:1:0', 'forget', 'visual:overview']);
    expect(r.frames[r.frames.length - 1]).toMatchObject({ kind: 'overview', main: 'home card' });
    expect(r.frames[r.frames.length - 1]).not.toHaveProperty('assistantOverlay');
    expect(r.hud.hasAssistantOverlay()).toBe(false);
  });

  it('streams appended text into the same page without another rebuild', async () => {
    const r = rig();
    expect(await r.hud.showAssistantOverlay('Timer started')).toBe(true);
    const rebuildsAfterOpen = r.rebuilds.length;
    expect(await r.hud.showAssistantOverlay(' for 25 minutes.', true)).toBe(true);
    await vi.waitFor(() => expect(r.upgrades.some((u) => (
      u.containerName === 'main' && u.content === 'Timer started for 25 minutes.'
    ))).toBe(true));
    expect(r.rebuilds).toHaveLength(rebuildsAfterOpen);
  });

  it('keeps a receipt on the image page', async () => {
    const r = rig();
    await r.hud.showText('home card', ContainerId.Main);
    const rebuildsBefore = r.rebuilds.length;
    expect(await r.hud.showAssistantOverlay('Saved QuickNote.', false, true, 'receipt')).toBe(true);
    expect(r.rebuilds).toHaveLength(rebuildsBefore);
    expect(r.frames[r.frames.length - 1]).toMatchObject({
      kind: 'overview', assistantOverlay: { text: 'Saved QuickNote.', presentation: 'receipt' },
    });
  });

  it('returns a firmware list directly to its own page after an answer', async () => {
    const r = rig();
    expect(await r.hud.showList(['Calendar create · Saved'], {
      title: 'ACTIVITY', hint: 'double-click: back', selectable: true, nativeFirmwareList: true,
    })).toBe(true);
    expect(await r.hud.showAssistantOverlay('Nothing else is pending.')).toBe(true);
    const before = r.operations.length;

    expect(await r.hud.dismissAssistantOverlay()).toBe(true);

    const since = r.operations.slice(before);
    expect(since.filter((op) => op.startsWith('rebuild:0:3:1'))).toHaveLength(1);
    expect(since.filter((op) => op.startsWith('rebuild:4:') || op.startsWith('visual:'))).toHaveLength(0);
  });
});
