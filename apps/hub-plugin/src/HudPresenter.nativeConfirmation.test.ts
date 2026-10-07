import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter, parseMessageDraft, setNativeOverviewForTest } from './HudPresenter';
import { ContainerId } from './types';
import type { HudVisualFrame } from './HudVisualSurface';

// These tests pin the IMAGE overview (VITE_NATIVE_OVERVIEW=0). The text
// overview, which is Home by default, has its own: HudPresenter.textOverview.test.ts.
setNativeOverviewForTest(false);

/**
 * The decision gate over the visual HUD is a firmware text page, not four
 * forced image tiles: Even's SDK draws a text container in one rebuild,
 * which is what lets "Send to X?" appear inside the Mac's confirmation
 * envelope. These cases pin that contract and the return to the image page.
 */
function rig(options: { replaceOk?: boolean } = {}) {
  const operations: string[] = [];
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const client = {
    require: () => ({
      rebuildPageContainer: vi.fn(async (page: RebuildPageContainer) => {
        operations.push(`rebuild:${page.imageObject?.length ?? 0}:${page.textObject?.length ?? 0}:${page.listObject?.length ?? 0}`);
        rebuilds.push(page);
        return options.replaceOk ?? true;
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
    paintExact: vi.fn(async (frame: HudVisualFrame, current: () => boolean) => {
      if (!current()) return false;
      operations.push(`exact:${frame.kind}`);
      frames.push(frame);
      return current();
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

describe('HudPresenter native confirmation over the visual HUD', () => {
  it('labels a spoken terminal reply check as send / say it again', async () => {
    const r = rig();
    expect(await r.hud.showConfirmation(
      'Send to work? “fix the login test”', 'terminal-input-1', undefined, undefined, 'terminal_input',
    )).toBe(true);
    const gate = r.rebuilds[r.rebuilds.length - 1];
    expect(text(gate, 'main')).toBe('CHECK WHAT WAS HEARD\n\nSend to work? “fix the login test”');
    expect(text(gate, 'context')).toBe('TAP · send   SWIPE · say it again');
  });

  it('shows the gate as one firmware text page carrying the live status, with no image tiles', async () => {
    const r = rig();
    await r.hud.showText('12:34 JOAT listening bat 80%', ContainerId.StatusBar);
    await r.hud.showText('home card', ContainerId.Main);
    const framesBefore = r.frames.length;

    expect(await r.hud.showConfirmation('Send to Adaeze? “On my way.”', 'send-1')).toBe(true);

    const gate = r.rebuilds[r.rebuilds.length - 1];
    expect(gate?.imageObject ?? []).toHaveLength(0);
    expect(gate?.listObject ?? []).toHaveLength(0);
    expect(gate?.textObject?.map((c) => c.containerName)).toEqual(['status', 'main', 'context', 'tray', 'hint']);
    expect(text(gate, 'main')).toBe('CONFIRM ACTION\n\nSend to Adaeze? “On my way.”');
    expect(text(gate, 'status')).toBe('12:34 JOAT listening bat 80%');
    expect(text(gate, 'hint')).toBe('8 seconds · timeout cancels');
    // Nothing was painted as pixels for the gate, and nothing repaints
    // behind it while it is open.
    expect(r.frames).toHaveLength(framesBefore);
    expect(await r.hud.showText('arrived while the gate was open', ContainerId.Main)).toBe(true);
    expect(r.frames).toHaveLength(framesBefore);
  });

  it('returns to the image page on dismissal: one visual rebuild, forget, then the retained overview', async () => {
    const r = rig();
    await r.hud.showText('home card', ContainerId.Main);
    await r.hud.showConfirmation('Send this message?', 'send-2');
    await r.hud.showText('reply completed behind the gate', ContainerId.Main);
    const before = r.operations.length;

    expect(await r.hud.dismissConfirmation('send-2')).toBe(true);

    expect(r.operations.slice(before)).toEqual(['rebuild:4:1:0', 'forget', 'visual:overview']);
    expect(r.frames[r.frames.length - 1]).toMatchObject({
      kind: 'overview', main: 'reply completed behind the gate',
    });
  });

  it('accepts a bound Messages gate on the rebuild receipt without an exact pixel paint', async () => {
    const r = rig();
    let current = true;
    expect(await r.hud.showConfirmation('Send the exact reviewed message?', 'bound-1', 'messages', () => current)).toBe(true);
    expect(r.visual.paintExact).not.toHaveBeenCalled();
    expect(r.rebuilds[r.rebuilds.length - 1]?.imageObject ?? []).toHaveLength(0);
    current = false;
    expect(await r.hud.dismissConfirmation('bound-1')).toBe(true);
  });

  it('does not arm a gate whose page replacement the host refused, and keeps Home interactive', async () => {
    const r = rig({ replaceOk: false });
    await r.hud.showText('home', ContainerId.Main);
    expect(await r.hud.showConfirmation('Send this message?', 'refused')).toBe(false);
    expect(r.operations.filter((op) => op === 'forget')).toHaveLength(0);
    expect(await r.hud.showText('after refused gate', ContainerId.Main)).toBe(true);
    expect(r.frames[r.frames.length - 1]).toMatchObject({ kind: 'overview', main: 'after refused gate' });
  });

  it('returns a firmware list directly to its own page, without an intermediate image rebuild', async () => {
    const r = rig();
    expect(await r.hud.showList(['New message', 'Adaeze · Call me'], {
      title: 'MESSAGES', hint: 'tap: select', selectable: true, nativeFirmwareList: true,
    })).toBe(true);
    await r.hud.showConfirmation('Send to Adaeze?', 'from-list');
    const before = r.operations.length;

    expect(await r.hud.dismissConfirmation('from-list')).toBe(true);

    const since = r.operations.slice(before);
    expect(since.filter((op) => op.startsWith('rebuild:4:'))).toHaveLength(0);
    expect(since.filter((op) => op.startsWith('rebuild:0:2:1'))).toHaveLength(1);
    expect(since.filter((op) => op.startsWith('visual:'))).toHaveLength(0);
  });
});

describe('parseMessageDraft', () => {
  it('splits the plain reply consent into recipient and body', () => {
    expect(parseMessageDraft('Send to Adele? “On my way, see you at 6.”')).toEqual({
      recipient: 'Adele', body: 'On my way, see you at 6.',
    });
  });

  it('splits the reviewed form, dropping the review marker from the body', () => {
    expect(parseMessageDraft('Send to Ada Lovelace? (reviewed) “The opening words…”')).toEqual({
      recipient: 'Ada Lovelace', body: 'The opening words…',
    });
  });

  it.each([
    'Send this message?',
    'Send the exact reviewed message?',
    'Send to Adele?',
    'Send to Adele? “   ”',
    'Send to work? "plain quotes"',
    'Delete to Adele? “words”',
  ])('returns undefined for anything else: %s', (prompt) => {
    expect(parseMessageDraft(prompt)).toBeUndefined();
  });
});

const CARD = ['status', 'conversation', 'wearer-turns', 'joat-turns', 'generation'];

describe('HudPresenter Messages reply in the conversation card', () => {
  it('draws a messages question as JOAT\'s turn in the conversation card, listening', async () => {
    const r = rig();
    expect(await r.hud.showPromptQuestion(
      'reply-question', 'What do you want to say back to Adele?', () => true, 'messages',
    )).toBe(true);
    const page = r.rebuilds[r.rebuilds.length - 1];
    expect(page?.imageObject ?? []).toHaveLength(0);
    expect(page?.textObject?.map((c) => c.containerName)).toEqual(CARD);
    expect(text(page, 'joat-turns')).toContain('What do you want to say back to Adele?');
    // Only JOAT's name stands in the dim layer, in front of its question.
    expect(text(page, 'wearer-turns')?.trim()).toBe('JOAT:');
    expect(text(page, 'generation')).toContain('listening');
    expect(text(page, 'generation')).toContain('double-click: cancel');
  });

  it('keeps an ordinary question on the plain question page', async () => {
    const r = rig();
    expect(await r.hud.showPromptQuestion('plain-question', 'What should I say?', () => true)).toBe(true);
    const page = r.rebuilds[r.rebuilds.length - 1];
    expect(page?.textObject?.map((c) => c.containerName)).toEqual(['status', 'main', 'hint']);
  });

  it.each([
    ['plain', 'Send to Adele? “On my way.”'],
    ['reviewed', 'Send to Adele? (reviewed) “On my way.”'],
  ])('draws a %s messages confirmation as the question, then the wearer\'s words, then the gestures', async (_form, prompt) => {
    const r = rig();
    expect(await r.hud.showConfirmation(prompt, 'reply-consent', 'messages')).toBe(true);
    const gate = r.rebuilds[r.rebuilds.length - 1];
    expect(gate?.imageObject ?? []).toHaveLength(0);
    expect(gate?.textObject?.map((c) => c.containerName)).toEqual(CARD);
    expect(text(gate, 'joat-turns')).toContain('What do you want to say back to Adele?');
    expect(text(gate, 'joat-turns')).not.toContain('On my way.');
    expect(text(gate, 'wearer-turns')).toContain('On my way.');
    expect(text(gate, 'wearer-turns')).not.toContain('(reviewed)');
    const footer = text(gate, 'generation') ?? '';
    expect(footer).toContain('tap: send');
    expect(footer).toContain('long-press: retry');
    expect(footer).toContain('double-click: cancel');
    expect(footer).not.toContain('hold');
  });

  it('keeps the plain decision page for a non-messages confirmation and an unparsed messages one', async () => {
    const r = rig();
    expect(await r.hud.showConfirmation('Send to Adele? “On my way.”', 'other-consent')).toBe(true);
    let gate = r.rebuilds[r.rebuilds.length - 1];
    expect(gate?.textObject?.map((c) => c.containerName)).toEqual(['status', 'main', 'context', 'tray', 'hint']);
    expect(text(gate, 'main')).toBe('CONFIRM ACTION\n\nSend to Adele? “On my way.”');
    expect(await r.hud.dismissConfirmation('other-consent')).toBe(true);

    expect(await r.hud.showConfirmation('Send the exact reviewed message?', 'unparsed', 'messages')).toBe(true);
    gate = r.rebuilds[r.rebuilds.length - 1];
    expect(gate?.textObject?.map((c) => c.containerName)).toEqual(['status', 'main', 'context', 'tray', 'hint']);
  });
});
