import {
  RebuildPageContainer,
  TextContainerProperty,
  type TextContainerUpgrade,
} from '@evenrealities/even_hub_sdk';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { BridgeClient } from './BridgeClient';
import {
  NATIVE_TERMINAL_GEOMETRY,
  NativeTerminalContainer,
  NativeTerminalSurface,
  isBrainstormThinking,
  nativeBrainstormFooter,
  nativeHeadText,
  type NativeTerminalPage,
} from './NativeTerminalSurface';
import type { BrainstormHudModel } from './types';

function brainstorm(overrides: Partial<BrainstormHudModel> = {}): BrainstormHudModel {
  return {
    screen: 'brainstorm',
    state: 'running',
    topic: 'Should JOAT cache embeddings on disk?',
    collaborators: ['Codex', 'Claude'],
    activeSpeaker: 'Claude · round 1/2 · 2 calls',
    activity: 'CODEX > Cache only with a size cap and an eviction rule\nCLAUDE > Measure hit rate first',
    ...overrides,
  };
}

function layout(page: Readonly<NativeTerminalPage>): RebuildPageContainer {
  const text = (id: number, name: string, y: number, height: number, capture: 0 | 1, content: string) =>
    new TextContainerProperty({
      containerID: id, containerName: name, xPosition: 0, yPosition: y,
      width: NATIVE_TERMINAL_GEOMETRY.width, height, paddingLength: NATIVE_TERMINAL_GEOMETRY.padding,
      borderWidth: 0, isEventCapture: capture, zOrderIndex: capture + 1, textColor: capture ? 4 : 2,
      content,
    });
  return new RebuildPageContainer({
    containerTotalNum: 2,
    textObject: [
      text(NativeTerminalContainer.Head.id, NativeTerminalContainer.Head.name,
        NATIVE_TERMINAL_GEOMETRY.headY, NATIVE_TERMINAL_GEOMETRY.headHeight, 0, page.headText),
      text(NativeTerminalContainer.Body.id, NativeTerminalContainer.Body.name,
        NATIVE_TERMINAL_GEOMETRY.bodyY, NATIVE_TERMINAL_GEOMETRY.bodyHeight, 1, page.bodyText),
    ],
  });
}

function rig() {
  const pages: NativeTerminalPage[] = [];
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const imageWrites = vi.fn(async () => 'success');
  const textContainerUpgrade = vi.fn(async (update: TextContainerUpgrade) => {
    upgrades.push(update);
    return true;
  });
  const client = {
    require: () => ({ textContainerUpgrade, updateImageRawData: imageWrites }),
  } as unknown as Pick<BridgeClient, 'require'>;
  const surface = new NativeTerminalSurface(client, (page) => {
    pages.push({ ...page });
    return layout(page);
  }, async (page) => {
    rebuilds.push(page);
    return true;
  });
  return { surface, pages, rebuilds, upgrades, imageWrites };
}

const lines = (page: NativeTerminalPage | undefined) => page?.bodyText.split('\n') ?? [];
/** What the body container shows now: the last upgrade, else the mounted page. */
const shown = (r: ReturnType<typeof rig>) => {
  const upgrade = r.upgrades[r.upgrades.length - 1];
  return upgrade ? String(upgrade.content).split('\n') : lines(r.pages[r.pages.length - 1]);
};

describe('native Brainstorm document', () => {
  afterEach(() => vi.useRealTimers());

  it('draws the room as firmware text: question in the head, state line, turns, hints', async () => {
    const { surface, pages, rebuilds, imageWrites } = rig();
    expect(await surface.present(brainstorm())).toBe(true);
    expect(rebuilds).toHaveLength(1);
    expect(rebuilds[0]?.imageObject ?? []).toHaveLength(0);
    expect(imageWrites).not.toHaveBeenCalled();
    const page = pages[0]!;
    expect(page.headText).toBe('BRAINSTORM · Should JOAT cache embeddings on disk?');
    const body = lines(page);
    expect(body).toHaveLength(NATIVE_TERMINAL_GEOMETRY.bodyLineCapacity);
    expect(body[0]).toBe('DISCUSSING · Claude · round 1/2 · 2 calls');
    expect(body).toContain('CODEX > Cache only with a size cap and an eviction rule');
    expect(body).toContain('CLAUDE > Measure hit rate first');
    expect(body[body.length - 1]).toBe('double-click: stop');
  });

  it('streams a growing turn as one body upgrade, never a rebuild', async () => {
    const { surface, rebuilds, upgrades } = rig();
    await surface.present(brainstorm());
    await surface.present(brainstorm({
      activity: 'CODEX > Cache only with a size cap\nCLAUDE > Measure hit rate first, because a cold',
    }));
    expect(rebuilds).toHaveLength(1);
    expect(upgrades).toHaveLength(1);
    expect(upgrades[0]?.containerName).toBe(NativeTerminalContainer.Body.name);
  });

  it('rebuilds once when a new question changes the head', async () => {
    const { surface, rebuilds } = rig();
    await surface.present(brainstorm({ state: 'ready', topic: 'Ready for a question', activity: '' }));
    await surface.present(brainstorm({ topic: 'A different question', activity: 'JOAT > Preparing collaborators…' }));
    expect(rebuilds).toHaveLength(2);
    expect(rebuilds[1]?.textObject?.[0]?.content).toBe('BRAINSTORM · A different question');
  });

  it('counts silence locally with body upgrades only', async () => {
    vi.useFakeTimers({ now: 1_000_000 });
    const r = rig();
    const { surface, rebuilds } = r;
    const thinking = brainstorm({ activity: 'CODEX > first point\nCLAUDE > Thinking…' });
    expect(isBrainstormThinking(thinking)).toBe(true);
    await surface.present(thinking);
    expect(surface.isThinking()).toBe(true);
    vi.setSystemTime(1_007_400);
    expect(surface.tick()).toBe(true);
    await surface.whenIdle();
    expect(shown(r)[0]).toBe('DISCUSSING · Claude · round 1/2 · 2 calls · 7s');
    expect(rebuilds).toHaveLength(1);
    await surface.present(brainstorm({ activity: 'CODEX > first point\nCLAUDE > The answer' }));
    expect(surface.isThinking()).toBe(false);
    expect(surface.tick()).toBe(false);
  });

  it('only offers gestures the Brainstorm controller accepts', () => {
    expect(nativeBrainstormFooter(brainstorm({ state: 'ready' }), 0, 0)).toBe('tap: ask a question · double-click: back');
    expect(nativeBrainstormFooter(brainstorm({ state: 'topic_capture' }), 0, 0))
      .toBe('Listening · speak your question · double-click: cancel');
    expect(nativeBrainstormFooter(brainstorm(), 3, 0)).toBe('double-click: stop · up: older');
    expect(nativeBrainstormFooter(brainstorm({ state: 'unavailable' }), 0, 0))
      .toBe('fix it on your phone: JOAT › Brainstorm');
    expect(nativeBrainstormFooter(brainstorm({ state: 'settled' }), 0, 0)).toBe('double-click: back');
  });

  it('keeps the plain title while no question is in the room', () => {
    expect(nativeHeadText(brainstorm({ state: 'ready', topic: 'Ready for a question' }))).toBe('JOAT · BRAINSTORM');
    expect(nativeHeadText(brainstorm({ screen: 'agent_terminal' }))).toBe('JOAT · AGENT TERMINAL');
  });

  it('scrolls a long room through older turns', async () => {
    const r = rig();
    const { surface } = r;
    const rows = Array.from({ length: 14 }, (_, i) => `${i % 2 ? 'CLAUDE' : 'CODEX'} > point ${i + 1}`);
    await surface.present(brainstorm({ activity: rows.join('\n') }));
    expect(shown(r)).toContain('CLAUDE > point 14');
    expect(surface.scroll('up')).toBe(true);
    await surface.whenIdle();
    expect(shown(r)).not.toContain('CLAUDE > point 14');
  });
});
