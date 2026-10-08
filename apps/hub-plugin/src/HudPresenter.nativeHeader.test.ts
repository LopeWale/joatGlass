import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import type { HudStatusModel } from './HudStatus';
import { ContainerId, type BrainstormHudModel } from './types';
import type { HudVisualFrame } from './HudVisualSurface';

// These tests pin the IMAGE overview (VITE_NATIVE_OVERVIEW=0). The text
// overview, which is Home by default, has its own: HudPresenter.textOverview.test.ts.
setNativeOverviewForTest(false);

/**
 * The firmware header is live. Until 2026-10-05 a clock, microphone or
 * battery change on a firmware page was dropped: `paint()` is a no-op
 * while the firmware owns the page, so the header froze at whatever the
 * rebuild carried (a list opened at 9:41 still said 9:41 at 10:05). The
 * presenter now holds the status as data and upgrades the one header
 * container in place — never a rebuild, never a tile.
 */
function rig() {
  const operations: string[] = [];
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const frames: HudVisualFrame[] = [];
  const client = {
    require: () => ({
      rebuildPageContainer: vi.fn(async (page: RebuildPageContainer) => {
        rebuilds.push(page);
        operations.push(`rebuild:${page.listObject?.length ?? 0}:${page.textObject?.length ?? 0}:${page.imageObject?.length ?? 0}`);
        return true;
      }),
      textContainerUpgrade: vi.fn(async (upgrade: TextContainerUpgrade) => {
        upgrades.push(upgrade);
        operations.push(`upgrade:${upgrade.containerName ?? upgrade.containerID}`);
        return true;
      }),
    }),
  } as unknown as BridgeClient;
  const visual = {
    paint: vi.fn(async (frame: HudVisualFrame) => { frames.push(frame); operations.push(`visual:${frame.kind}`); return true; }),
    paintExact: vi.fn(async (frame: HudVisualFrame, current: () => boolean) => {
      if (!current()) return false;
      frames.push(frame);
      operations.push(`visual:${frame.kind}`);
      return current();
    }),
    forgetPage: vi.fn(() => operations.push('forget')),
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  const since = (mark: number) => operations.slice(mark);
  return { hud, operations, rebuilds, upgrades, frames, since };
}

const at941: HudStatusModel = { clock: '9:41', link: 'connected', mic: false, battery: 87, charging: false };
const at942: HudStatusModel = { ...at941, clock: '9:42' };

function status(page: RebuildPageContainer | undefined): string | undefined {
  return page?.textObject?.find((c) => c.containerName === 'status')?.content;
}

function last<T>(values: readonly T[]): T | undefined {
  return values[values.length - 1];
}

describe('the status model on the visual page', () => {
  it('rides the frame as data, with the untitled line in the stream for the text paths', async () => {
    const r = rig();
    expect(await r.hud.setStatusModel(at941)).toBe(true);
    expect(last(r.frames)).toMatchObject({ kind: 'overview', statusModel: at941, status: '9:41 · JOAT · ○ · ▇' });
    expect(r.upgrades).toEqual([]);
    expect(r.rebuilds).toEqual([]);
    // The microphone flag the renderer already reads follows the model.
    await r.hud.setStatusModel({ ...at941, mic: true });
    expect(last(r.frames)).toMatchObject({ micOn: true, statusModel: { mic: true } });
  });
});

describe('the header on a firmware list page', () => {
  it('is composed with the page\'s name and refreshed in place, once per change', async () => {
    const r = rig();
    await r.hud.setStatusModel(at941);
    expect(await r.hud.showList(['Calendar create · Saved'], {
      title: 'ACTIVITY', hint: '2x tap: back', selectable: true, nativeFirmwareList: true,
    })).toBe(true);
    expect(status(last(r.rebuilds))).toBe('9:41 · ACTIVITY · ○ · ▇');
    const mark = r.operations.length;

    expect(await r.hud.setStatusModel(at942)).toBe(true);
    expect(r.since(mark)).toEqual(['upgrade:status']);
    expect(last(r.upgrades)).toMatchObject({
      containerID: ContainerId.StatusBar, containerName: 'status', contentOffset: 0,
      content: '9:42 · ACTIVITY · ○ · ▇',
    });
    // The overwrite length covers the longer of old and new, so a shorter
    // line clears the old tail.
    expect(last(r.upgrades)?.contentLength).toBe('9:42 · ACTIVITY · ○ · ▇'.length);

    // The same facts again cost nothing.
    const again = r.operations.length;
    expect(await r.hud.setStatusModel(at942)).toBe(true);
    expect(r.since(again)).toEqual([]);

    // The mic opening, the Mac speaking, the battery charging: each is
    // one header upgrade, never a rebuild, never a tile.
    await r.hud.setStatusModel({ ...at942, mic: true });
    expect(last(r.upgrades)?.content).toBe('9:42 · ACTIVITY · ● · ▇');
    await r.hud.setStatusModel({ ...at942, mic: true, macState: 'hearing you…' });
    expect(last(r.upgrades)?.content).toBe('9:42 · JOAT · HEARING YOU… · ● · ▇');
    await r.hud.setStatusModel({ ...at942, charging: true });
    expect(last(r.upgrades)?.content).toBe('9:42 · ACTIVITY · ○ · ▇+');
    expect(r.rebuilds).toHaveLength(1);
    expect(r.operations.filter((op) => op.startsWith('visual:'))).toHaveLength(1); // the first model, at home
  });

  it('returns to painting frames once the visual page is back', async () => {
    const r = rig();
    await r.hud.setStatusModel(at941);
    await r.hud.showList(['row'], { title: 'ACTIVITY', hint: '2x tap: back', selectable: true, nativeFirmwareList: true });
    expect(await r.hud.showDefault()).toBe(true);
    const mark = r.operations.length;
    await r.hud.setStatusModel(at942);
    expect(r.since(mark)).toEqual(['visual:overview']);
    expect(last(r.frames)).toMatchObject({ statusModel: at942 });
  });
});

describe('the header on a firmware text page', () => {
  it('names the reader and keeps its clock moving in place', async () => {
    const r = rig();
    await r.hud.setStatusModel(at941);
    expect(await r.hud.showList(['1/1 · From Dele', 'Call me when you arrive.'], {
      title: 'MESSAGES · MESSAGE', hint: '2x tap: back', selectable: false, nativeTextPage: true,
    })).toBe(true);
    expect(status(last(r.rebuilds))).toBe('9:41 · MESSAGES · MESSAGE · ○ · ▇');
    const mark = r.operations.length;
    await r.hud.setStatusModel(at942);
    expect(r.since(mark)).toEqual(['upgrade:status']);
    expect(last(r.upgrades)?.content).toBe('9:42 · MESSAGES · MESSAGE · ○ · ▇');
  });
});

describe('the head line of the native terminal page', () => {
  const session: BrainstormHudModel = {
    screen: 'brainstorm', state: 'running', topic: 'Should the glasses cache the picker?',
    collaborators: ['Codex', 'Claude'], activeSpeaker: 'Codex · round 1/3 · 2 calls',
    activity: 'YOU > Should the glasses cache the picker?\nCODEX > A cache saves a round trip.',
  };

  it('is the status header, upgraded in place when the facts change', async () => {
    const r = rig();
    await r.hud.setStatusModel(at941);
    expect(await r.hud.showBrainstorm(session)).toBe(true);
    const page = last(r.rebuilds);
    expect(page?.textObject?.find((c) => c.containerName === 'terminal-head')?.content)
      .toBe('9:41 · BRAINSTORM · ○ · ▇');
    const mark = r.operations.length;
    expect(await r.hud.setStatusModel(at942)).toBe(true);
    expect(r.since(mark)).toEqual(['upgrade:terminal-head']);
    expect(last(r.upgrades)?.content).toBe('9:42 · BRAINSTORM · ○ · ▇');
    expect(r.rebuilds).toHaveLength(1);
  });
});
