import {
  utf8ByteLength,
  validateEvenHubPageContainer,
  type RebuildPageContainer,
  type TextContainerUpgrade,
} from '@evenrealities/even_hub_sdk';
import { getTextWidth } from '@evenrealities/pretext';
import { describe, expect, it, vi } from 'vitest';
import type { BridgeClient } from './BridgeClient';
import { TEXT_CONTAINER_MAX_BYTES } from './FirmwareLimits';
import { firmwareLineWidth } from './FirmwareText';
import { HudPageBuilder } from './HudPageBuilder';
import {
  NATIVE_TERMINAL_GEOMETRY,
  NativeTerminalContainer,
  NativeTerminalSurface,
  TERMINAL_WORKING_SPINNERS,
  buildApprovalBox,
  formatToolLine,
  type NativeTerminalPage,
} from './NativeTerminalSurface';
import type { AgentTerminalHudModel, TerminalConversationMessage } from './types';

const builder = new HudPageBuilder();

function terminal(overrides: Partial<AgentTerminalHudModel> = {}): AgentTerminalHudModel {
  return {
    state: 'ready',
    focus: 'Claude · idle',
    lanes: ['Claude · idle', 'Codex · idle', 'Antigravity · idle'],
    activity: 'Ready',
    conversationId: 'claude-session-1',
    messages: [],
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

function assertAlignedDualLayers(page: NativeTerminalPage): void {
  expect(page.dimLines).toHaveLength(7);
  expect(page.brightLines).toHaveLength(7);
  expect(page.dimText.split('\n')).toHaveLength(7);
  expect(page.brightText.split('\n')).toHaveLength(7);
  for (let index = 0; index < 7; index += 1) {
    const dimRow = page.dimLines[index]!;
    const brightRow = page.brightLines[index]!;
    expect(dimRow.length).toBeGreaterThan(0);
    expect(brightRow.length).toBeGreaterThan(0);
    // Mutual exclusivity: a content row never draws on both layers simultaneously.
    expect(dimRow !== ' ' && brightRow !== ' ').toBe(false);
    expect(firmwareLineWidth(dimRow)).toBeLessThanOrEqual(NATIVE_TERMINAL_GEOMETRY.bodyTextWidth);
    expect(firmwareLineWidth(brightRow)).toBeLessThanOrEqual(NATIVE_TERMINAL_GEOMETRY.bodyTextWidth);
    expect(getTextWidth(dimRow)).toBeLessThanOrEqual(NATIVE_TERMINAL_GEOMETRY.bodyTextWidth);
    expect(getTextWidth(brightRow)).toBeLessThanOrEqual(NATIVE_TERMINAL_GEOMETRY.bodyTextWidth);
  }
  for (const text of [page.headText, page.dimText, page.brightText, page.footText]) {
    expect(utf8ByteLength(text)).toBeLessThanOrEqual(TEXT_CONTAINER_MAX_BYTES);
  }
}

interface RigOptions {
  upgrade?: (update: TextContainerUpgrade) => Promise<boolean>;
  imageHeader?: () => boolean;
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
      pages.push({ ...page, dimLines: [...page.dimLines], brightLines: [...page.brightLines] });
      return builder.nativeTerminalLayout(page);
    },
    replacePage,
    { imageHeader: options.imageHeader },
  );
  return { surface, pages, rebuilds, upgrades, imageWrites, textContainerUpgrade, replacePage };
}

function last<T>(values: readonly T[]): T | undefined {
  return values[values.length - 1];
}

describe('NativeTerminalSurface 5-container framed dual-brightness card', () => {
  it('mounts the exact 5-container card layout once and uses selective TextContainerUpgrades afterwards', async () => {
    const { surface, rebuilds, upgrades, imageWrites } = rig();

    expect(await surface.present(terminal(), '3:45 · CLAUDE · ● · ▇')).toBe(true);
    expect(rebuilds).toHaveLength(1);
    expect(upgrades).toHaveLength(0);
    expect(imageWrites).not.toHaveBeenCalled();

    const mounted = rebuilds[0]!;
    expect(validateEvenHubPageContainer(mounted)).toEqual({ valid: true });
    expect(mounted.containerTotalNum).toBe(5);
    expect(mounted.imageObject ?? []).toHaveLength(0);
    expect(mounted.listObject ?? []).toHaveLength(0);
    expect(mounted.textObject?.map((item) => item.containerName)).toEqual([
      'terminal-head',
      'terminal-card',
      'terminal-dim',
      'terminal-bright',
      'terminal-foot',
    ]);
    expect(mounted.textObject?.filter((item) => item.isEventCapture === 1).map((item) => item.containerName))
      .toEqual(['terminal-card']);

    const byName = (name: string) => mounted.textObject?.find((item) => item.containerName === name);
    expect(byName('terminal-head')).toMatchObject({
      containerID: NativeTerminalContainer.Head.id,
      xPosition: 0,
      yPosition: 0,
      width: 576,
      height: 32,
      textColor: 2,
      isEventCapture: 0,
    });
    expect(byName('terminal-card')).toMatchObject({
      containerID: 12,
      xPosition: 12,
      yPosition: 34,
      width: 552,
      height: 214,
      borderWidth: 1,
      borderColor: 8,
      borderRadius: 4,
      textColor: 0,
      isEventCapture: 1,
      content: ' ',
    });
    expect(byName('terminal-dim')).toMatchObject({
      containerID: 13,
      xPosition: 24,
      yPosition: 42,
      width: 528,
      height: 198,
      borderWidth: 0,
      textColor: 2,
      isEventCapture: 0,
    });
    expect(byName('terminal-bright')).toMatchObject({
      containerID: 14,
      xPosition: 24,
      yPosition: 42,
      width: 528,
      height: 198,
      borderWidth: 0,
      textColor: 4,
      isEventCapture: 0,
    });
    expect(byName('terminal-foot')).toMatchObject({
      containerID: NativeTerminalContainer.Foot.id,
      xPosition: 12,
      yPosition: 252,
      width: 552,
      height: 34,
      borderWidth: 0,
      textColor: 2,
      isEventCapture: 0,
    });

    const initial = surface.renderedPage()!;
    assertAlignedDualLayers(initial);
    expect(initial.headText).toBe('3:45 · CLAUDE · ● · ▇');
    expect(initial.dimLines[0]).toBe('Tap to speak to Claude.');
    expect(initial.brightLines.every((row) => row === ' ')).toBe(true);
    expect(initial.footText).toBe('tap: speak · 2x: sessions');

    // Streaming assistant prose updates Dim, Bright, and Foot without rebuilding or touching Head/Card.
    expect(await surface.present(terminal({
      state: 'running',
      messages: [
        { id: 'u1', role: 'user', text: 'Inspect package.json and check scripts.' },
        { id: 'a1', role: 'assistant', text: '⏺ read_file: apps/hub-plugin/package.json\nVitest and TypeScript 7 are configured.' },
      ],
    }))).toBe(true);
    expect(rebuilds).toHaveLength(1);
    expect(upgrades.map((u) => u.containerName)).toEqual([
      'terminal-dim',
      'terminal-bright',
      'terminal-foot',
    ]);
    const runningPage = surface.renderedPage()!;
    assertAlignedDualLayers(runningPage);
    expect(runningPage.dimText).toContain('> Inspect package.json and check scripts.');
    expect(runningPage.dimText).toContain('>> read_file · apps/hub-plugin/package.json');
    expect(runningPage.brightText).toContain('Vitest and TypeScript 7 are configured.');
    expect(runningPage.brightText).not.toContain('AGENT >');
    expect(runningPage.dimText).not.toContain('YOU >');
    expect(runningPage.footText).toBe('◑ Working · hold: stop · 2x: sessions');
  });

  it('aligns dual layers across user hanging indents, 1-line tool calls, notices, and assistant prose separators', async () => {
    const { surface } = rig();
    expect(await surface.present(terminal({
      state: 'settled',
      messages: [
        {
          id: 'u1',
          role: 'user',
          text: 'Explain how the dual brightness layers align inside the framed session card when lines wrap across rows.',
        },
        {
          id: 'a1',
          role: 'assistant',
          text: [
            '⏺ read_file: src/TerminalController.ts',
            '⏺ Bash(npm test -- --run NativeTerminalSurface)',
            'Dim rows carry prompts and tool calls while Bright rows carry prose.',
          ].join('\n'),
        },
        {
          id: 'u2',
          role: 'user',
          text: 'Run typecheck next.',
        },
      ],
    }))).toBe(true);

    const page = surface.renderedPage()!;
    assertAlignedDualLayers(page);
    // Separator blank row sits between assistant prose and the next `> ` user turn.
    const u2Index = page.dimLines.findIndex((line) => line === '> Run typecheck next.');
    expect(u2Index).toBeGreaterThan(0);
    expect(page.dimLines[u2Index - 1]).toBe(' ');
    expect(page.brightLines[u2Index - 1]).toBe(' ');
    expect(page.dimText).toContain('>> read_file · src/TerminalController.ts');
    expect(page.dimText).toContain('>> Bash · npm test -- --run NativeTerminalSurface');
    expect(formatToolLine('⏺ run_command: ' + 'very/deeply/nested/path/segment/'.repeat(12), 528))
      .toMatch(/^>> run_command · /);
    expect(firmwareLineWidth(formatToolLine('⏺ run_command: ' + 'very/deeply/nested/path/segment/'.repeat(12), 528)))
      .toBeLessThanOrEqual(528);
  });

  it('strips model names from headers and status copy', async () => {
    const { surface } = rig();
    expect(await surface.present(
      terminal({
        focus: 'claude-sonnet-4-5 · ready',
        state: 'unavailable',
        terminalReason: 'Model claude-sonnet-4-5 (gemini-3.5-flash-lite / gpt-5.6-terra) requires login.',
      }),
      '3:45 · claude-sonnet-4-5 · ● · ▇',
    )).toBe(true);
    const page = surface.renderedPage()!;
    assertAlignedDualLayers(page);
    const allText = [page.headText, page.dimText, page.brightText, page.footText].join('\n');
    expect(allText).not.toMatch(/claude-sonnet|gemini-3\.5|gpt-5\.6/i);
    expect(page.dimLines[0]).toMatch(/^! /);
    expect(page.footText).toBe('tap: retry · 2x: sessions');
  });

  it('renders a firmware-measured <= 528px Unicode approval box on Bright and dims context above it without rebuilding', async () => {
    const { surface, rebuilds, upgrades } = rig();
    expect(await surface.present(terminal({
      state: 'running',
      messages: [
        { id: 'u1', role: 'user', text: 'Check repo status and clean build artifacts.' },
        { id: 'a1', role: 'assistant', text: 'I will check git status before removing build artifacts.' },
      ],
    }))).toBe(true);
    expect(rebuilds).toHaveLength(1);

    const longCommand = 'git status --porcelain=v2 --branch --ignored=matching apps/hub-plugin/src/NativeTerminalSurface.ts apps/mac/joat/services/agent_terminal.py';
    expect(await surface.present(terminal({
      state: 'running',
      approvals: 1,
      messages: [
        { id: 'u1', role: 'user', text: 'Check repo status and clean build artifacts.' },
        { id: 'a1', role: 'assistant', text: 'I will check git status before removing build artifacts.' },
        { id: 's1', role: 'status', text: `[approval] run_command: ${longCommand} — say 'terminal approve' or 'terminal deny'` },
      ],
    }))).toBe(true);

    expect(rebuilds).toHaveLength(1);
    expect(upgrades.length).toBeGreaterThan(0);

    const page = surface.renderedPage()!;
    assertAlignedDualLayers(page);
    expect(page.footText).toBe('tap: approve · swipe: deny · 2x: sessions');

    const boxRows = page.brightLines.filter((line) => line !== ' ');
    expect(boxRows.length).toBeGreaterThanOrEqual(3);
    expect(boxRows[0]).toMatch(/^╭── Approve command ─+╮$/);
    expect(boxRows[1]).toMatch(/^│ run_command · git status/);
    expect(boxRows[boxRows.length - 1]).toMatch(/^╰─+╯$/);
    for (const row of boxRows) {
      expect(firmwareLineWidth(row)).toBeLessThanOrEqual(528);
      expect(getTextWidth(row)).toBeLessThanOrEqual(528);
    }

    // Prior assistant prose above the approval box is dimmed onto Dim, not Bright!
    const firstBoxIdx = page.brightLines.findIndex((line) => line.startsWith('╭'));
    expect(firstBoxIdx).toBeGreaterThan(0);
    for (let i = 0; i < firstBoxIdx; i += 1) {
      expect(page.brightLines[i]).toBe(' ');
    }
    expect(page.dimLines.slice(0, firstBoxIdx).join(' ')).toContain('removing build artifacts');
    expect(surface.scroll('up')).toBe(false);

    // Direct helper check on pathological unbroken strings
    const unbrokenBox = buildApprovalBox('W'.repeat(240), 2, 528);
    expect(unbrokenBox[0]).toContain('Approve command (2)');
    for (const line of unbrokenBox) {
      expect(firmwareLineWidth(line)).toBeLessThanOrEqual(528);
      expect(getTextWidth(line)).toBeLessThanOrEqual(528);
    }
  });

  it('streams inline voice partials (<= 4 rows, no QUESTION > or YOU >) and transitions to transcribing via upgrades only', async () => {
    const { surface, rebuilds } = rig();
    expect(await surface.present(terminal({
      focus: 'Antigravity · ready',
      messages: [
        { id: 'u1', role: 'user', text: 'Previous prompt' },
        { id: 'a1', role: 'assistant', text: 'Previous assistant answer.' },
      ],
    }))).toBe(true);

    expect(await surface.showPrompt('p_voice', 'Speak to Antigravity.', () => true)).toBe(true);
    expect(rebuilds).toHaveLength(1);
    let page = surface.renderedPage()!;
    assertAlignedDualLayers(page);
    expect(page.brightText).not.toContain('QUESTION >');
    expect(page.brightText).not.toContain('YOU >');
    expect(page.brightLines).toContain('> Listening…');
    expect(page.footText).toBe('● Listening · tap: done · 2x: cancel');
    expect(surface.scroll('up')).toBe(false);

    expect(await surface.showPromptPartial('p_voice', 'Check git status and summarize')).toBe(true);
    expect(rebuilds).toHaveLength(1);
    page = surface.renderedPage()!;
    assertAlignedDualLayers(page);
    expect(page.brightLines).toContain('> Check git status and summarize');
    expect(page.brightText).not.toContain('QUESTION >');
    expect(page.brightText).not.toContain('YOU >');

    // Long speech tail-slices to <= 4 rows on Bright while keeping context above.
    const longSpeech = Array.from({ length: 22 }, (_, index) => `phrase${index + 1} inspecting files`).join(' ');
    expect(await surface.showPromptPartial('p_voice', longSpeech)).toBe(true);
    page = surface.renderedPage()!;
    assertAlignedDualLayers(page);
    const promptSlice = page.brightLines.slice(-4);
    expect(promptSlice[0]).toBe('> …');
    expect(promptSlice.join(' ')).toContain('phrase22');
    expect(page.dimLines.slice(0, 3).some((line) => line !== ' ')).toBe(true);

    // Tap-to-done shows transcribing in footer while keeping spoken words visible.
    expect(await surface.showPromptTranscribing('p_voice')).toBe(true);
    page = surface.renderedPage()!;
    assertAlignedDualLayers(page);
    expect(page.footText).toBe('◐ Transcribing… · 2x: cancel');
    expect(page.brightLines.join(' ')).toContain('phrase22');

    // Empty partial resets back to `> Listening…`
    expect(await surface.showPromptPartial('p_voice', '')).toBe(true);
    page = surface.renderedPage()!;
    expect(page.brightLines).toContain('> Listening…');
    expect(page.footText).toBe('● Listening · tap: done · 2x: cancel');

    expect(await surface.dismissPrompt('p_voice')).toBe(true);
    page = surface.renderedPage()!;
    expect(page.brightLines).not.toContain('> Listening…');
    expect(page.footText).toBe('tap: speak · 2x: sessions');
    expect(rebuilds).toHaveLength(1);
  });

  it('cycles working spinner frames and elapsed seconds via terminal-foot upgrades ONLY', async () => {
    vi.useFakeTimers({ now: 2_000_000 });
    try {
      const { surface, rebuilds, upgrades } = rig();
      expect(await surface.present(terminal({
        focus: 'Codex · working',
        state: 'running',
        messages: [{ id: 'm1', role: 'user', text: 'Run unit tests.' }],
      }))).toBe(true);
      expect(surface.isThinking()).toBe(true);
      const upgradesBeforeTick = upgrades.length;

      vi.setSystemTime(2_001_100);
      expect(surface.tick()).toBe(true);
      await surface.whenIdle();
      expect(rebuilds).toHaveLength(1);
      // ONLY `terminal-foot` changed — zero writes to Head, Dim, or Bright!
      expect(upgrades.slice(upgradesBeforeTick).map((u) => u.containerName)).toEqual(['terminal-foot']);
      expect(last(upgrades)?.content).toBe('◒ Working · 1s · hold: stop · 2x: sessions');

      const seenSpinners = new Set<string>();
      for (let step = 2; step <= 5; step += 1) {
        vi.setSystemTime(2_000_000 + step * 1000);
        expect(surface.tick()).toBe(true);
        await surface.whenIdle();
        const glyph = (last(upgrades)?.content ?? '')[0]!;
        seenSpinners.add(glyph);
      }
      expect([...seenSpinners].sort()).toEqual([...TERMINAL_WORKING_SPINNERS].sort());
    } finally {
      vi.useRealTimers();
    }
  });

  it('supports scrollback, omits swipe: scroll when short, and restores per-conversation reading anchors', async () => {
    const { surface, rebuilds } = rig();
    const claude = terminal({
      focus: 'Claude · settled',
      state: 'settled',
      conversationId: 'claude-conv',
      messages: [
        ...longTranscript('Claude'),
        { id: 'wide', role: 'assistant', text: 'W'.repeat(260) },
      ],
    });

    expect(await surface.present(claude)).toBe(true);
    const tail = surface.renderedPage()!;
    assertAlignedDualLayers(tail);
    expect(tail.older).toBeGreaterThan(0);
    expect(tail.newer).toBe(0);
    expect(tail.footText).toBe('tap: speak · swipe: scroll · 2x: sessions');
    expect(surface.scroll('down')).toBe(false);
    expect(surface.scroll('up')).toBe(true);
    await surface.whenIdle();

    const scrolled = surface.renderedPage()!;
    assertAlignedDualLayers(scrolled);
    expect(scrolled.newer).toBeGreaterThan(0);
    expect(scrolled.footText).toContain(`▼ ${scrolled.newer} newer`);
    expect(scrolled.footText).toContain('swipe: scroll · 2x: sessions');

    while (surface.scroll('up')) await surface.whenIdle();
    expect(surface.scroll('up')).toBe(false);

    expect(await surface.pauseForHandoff()).toBe(true);
    expect(surface.completeHandoff()).toBe(true);
    expect(await surface.present(terminal({
      focus: 'Codex · ready',
      conversationId: 'codex-conv',
      messages: longTranscript('Codex'),
    }))).toBe(true);
    expect(await surface.pauseForHandoff()).toBe(true);
    expect(surface.completeHandoff()).toBe(true);
    expect(await surface.present(claude)).toBe(true);
    expect(rebuilds).toHaveLength(3);
    const reopened = surface.renderedPage()!;
    assertAlignedDualLayers(reopened);
    expect(reopened.older).toBe(0);
    expect(reopened.newer).toBeGreaterThan(0);
  });

  it('coalesces queued stream revisions while keeping native writes serial and recovers a missing page once', async () => {
    let firstRelease: ((accepted: boolean) => void) | undefined;
    const first = new Promise<boolean>((resolve) => { firstRelease = resolve; });
    let calls = 0;
    let inFlight = 0;
    let maxInFlight = 0;
    const { surface, upgrades } = rig({
      upgrade: async () => {
        calls += 1;
        inFlight += 1;
        maxInFlight = Math.max(maxInFlight, inFlight);
        const result = calls === 1 ? await first : true;
        inFlight -= 1;
        return result;
      },
    });
    expect(await surface.present(terminal())).toBe(true);

    const rev = (id: string, text: string) => terminal({
      state: 'running',
      messages: [{ id, role: 'assistant', text }],
    });
    const one = surface.present(rev('one', 'first chunk'));
    await vi.waitFor(() => expect(upgrades.length).toBeGreaterThanOrEqual(1));
    const two = surface.present(rev('two', 'intermediate chunk'));
    const three = surface.present(rev('three', 'final streamed chunk'));
    firstRelease?.(true);

    expect(await one).toBe(false);
    expect(await two).toBe(false);
    expect(await three).toBe(true);
    await surface.whenIdle();
    expect(maxInFlight).toBe(1);
    const brightWrites = upgrades.filter((u) => u.containerName === 'terminal-bright');
    expect(last(brightWrites)?.content).toContain('final streamed chunk');
    expect(brightWrites.some((u) => u.content?.includes('intermediate chunk'))).toBe(false);

    // Missing native container triggers a single 5-container rebuild recovery.
    let failOnce = true;
    const recovery = rig({
      upgrade: async () => {
        if (failOnce) {
          failOnce = false;
          return false;
        }
        return true;
      },
    });
    expect(await recovery.surface.present(terminal())).toBe(true);
    expect(await recovery.surface.present(rev('recover', 'Recovered after host drop.'))).toBe(true);
    expect(recovery.rebuilds).toHaveLength(2);
    expect(recovery.rebuilds[1]?.textObject?.map((c) => c.containerName)).toEqual([
      'terminal-head', 'terminal-card', 'terminal-dim', 'terminal-bright', 'terminal-foot',
    ]);
  });

  it('strips markdown file links and U+0060 backticks and omits resolved [approval] rows', async () => {
    const { surface } = rig();
    expect(await surface.present(terminal({
      state: 'ready',
      approvals: 0,
      messages: [
        { id: 'u1', role: 'user', text: 'Check pyproject and branch' },
        { id: 's1', role: 'status', text: "[approval] run_command: git rev-parse --abbrev-ref HEAD — say 'terminal approve' or 'terminal deny'" },
        { id: 'a1', role: 'assistant', text: '⏺ run_command: git rev-parse --abbrev-ref HEAD\nThe name in [pyproject.toml](file:///Users/test/pyproject.toml) is `joat-g2` on branch `main`.' },
      ],
    }))).toBe(true);
    const page = surface.renderedPage()!;
    assertAlignedDualLayers(page);
    expect(page.dimText).not.toContain('[approval]');
    expect(page.dimText).toContain('>> run_command · git rev-parse --abbrev-ref HEAD');
    expect(page.brightText).not.toContain('file://');
    expect(page.brightText).not.toContain('`');
    expect(page.brightText).toContain('The name in pyproject.toml is joat-g2 on branch main.');
  });
});
