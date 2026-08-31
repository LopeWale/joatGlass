import { describe, expect, it, vi } from 'vitest';

import {
  ASSISTANT_OVERLAY_VISUAL_TEXT,
  VISUAL_CONFIRMATION_PROMPT,
  brainstormVisualFixture,
  isBrainstormVisualState,
  runVisualConfirmationFixture,
  isProductivityVisualState,
  stitchHudPreviewRgba,
  visualListFixture,
} from './VisualFixtures';

describe('visual fixtures', () => {
  it('uses realistic content-free copy for the assistant overlay lens check', () => {
    expect(ASSISTANT_OVERLAY_VISUAL_TEXT).toContain('calendar and reminders');
    expect(ASSISTANT_OVERLAY_VISUAL_TEXT).toContain('monitoring is armed');
    expect(ASSISTANT_OVERLAY_VISUAL_TEXT).not.toContain('Adele');
  });

  it('stitches renderer tiles into one native-size green optical preview', () => {
    const pixels = (value: number) => new Uint8Array(288 * 144).fill(value);
    const rgba = stitchHudPreviewRgba([
      { containerId: 4, pixels: pixels(17) },
      { containerId: 8, pixels: pixels(34) },
      { containerId: 9, pixels: pixels(51) },
      { containerId: 10, pixels: pixels(68) },
    ]);
    const greenAt = (x: number, y: number) => rgba[(y * 576 + x) * 4 + 1];
    expect(rgba).toHaveLength(576 * 288 * 4);
    expect(greenAt(0, 0)).toBe(17);
    expect(greenAt(575, 0)).toBe(34);
    expect(greenAt(0, 287)).toBe(51);
    expect(greenAt(575, 287)).toBe(68);
    for (let offset = 0; offset < rgba.length; offset += 4) {
      expect(rgba[offset]).toBe(0);
      expect(rgba[offset + 2]).toBe(0);
      expect(rgba[offset + 3]).toBe(255);
    }
  });

  it('covers every productivity collection and durable write receipt', () => {
    expect(visualListFixture('notifications').title).toBe('NOTIFICATIONS');
    expect(visualListFixture('calendar').title).toBe('CALENDAR · 7 DAYS');
    expect(visualListFixture('tasks').title).toBe('TASKS');
    expect(visualListFixture('quicknotes').title).toBe('QUICKNOTES');
    expect(visualListFixture('activity').title).toBe('ACTIVITY');
    expect(visualListFixture('action')).toEqual({
      title: 'ACTION · DONE',
      hint: 'left x2 overview',
      items: ['Calendar create · Saved and verified'],
      selectable: false,
    });
    expect(visualListFixture('messages').selectable).toBe(true);
    expect(visualListFixture('notification-detail').selectable).toBe(false);
    expect(visualListFixture('calendar-detail').title).toBe('CALENDAR · DETAIL');
    expect(visualListFixture('task-detail').title).toBe('TASKS · DETAIL');
    expect(visualListFixture('quicknote-detail').title).toBe('QUICKNOTES · NOTE');
    // A recording's summary page carries the distilled sections in order.
    expect(visualListFixture('quicknote-detail').items).toContain('SUMMARY');
    expect(visualListFixture('quicknote-detail').items).toContain('KEY POINTS');
    expect(visualListFixture('quicknote-detail').items).toContain('TODOS');
    // The live recorder owns the QuickNotes page with tap-only controls.
    expect(visualListFixture('quicknote-recording')).toMatchObject({
      title: 'QUICKNOTES · RECORDING',
      selectable: true,
      items: ['Save and finish', 'Pause recording', 'Discard recording'],
    });
    expect(visualListFixture('quicknote-discard').items).toEqual(
      ['Confirm discard', 'Keep recording'],
    );
    expect(visualListFixture('quicknote-saving').selectable).toBe(false);
    expect(visualListFixture('activity-detail').title).toBe('ACTIVITY · DETAIL');
    expect(isProductivityVisualState('notification-detail')).toBe(true);
    expect(isProductivityVisualState('overview')).toBe(false);
  });

  it('covers real Brainstorm and Hermes terminal visual states', () => {
    expect(brainstormVisualFixture('brainstorm-live')).toMatchObject({
      screen: 'brainstorm', state: 'running', activeSpeaker: 'Hermes · terminal',
    });
    expect(brainstormVisualFixture('agent-terminal-ready')).toMatchObject({
      screen: 'agent_terminal', state: 'ready', noToolsChecked: true,
    });
    expect(brainstormVisualFixture('agent-terminal-blocked')).toMatchObject({
      screen: 'agent_terminal', state: 'blocked', noToolsChecked: false,
    });
    expect(isBrainstormVisualState('brainstorm-ready')).toBe(true);
    expect(isBrainstormVisualState('overview')).toBe(false);
  });

  it('makes the confirmation fixture unmistakably test-only', () => {
    expect(VISUAL_CONFIRMATION_PROMPT).toContain('SIMULATOR TEST (NO SEND)');
    expect(VISUAL_CONFIRMATION_PROMPT).toContain('No message will be sent');
    expect(VISUAL_CONFIRMATION_PROMPT).not.toContain('Adele');
    expect(VISUAL_CONFIRMATION_PROMPT).not.toContain('mockups');
  });

  it('arms the fixture gate and dismisses it with the simulator verdict', async () => {
    let settle: ((accepted: boolean) => void) | undefined;
    const hud = {
      showConfirmation: vi.fn(async () => true),
      dismissConfirmation: vi.fn(async () => true),
    };
    const imu = {
      prepare: vi.fn((_actionId: string, onResult: (accepted: boolean) => void) => {
        settle = onResult;
      }),
      activate: vi.fn(() => true),
      cancel: vi.fn(() => true),
    };

    await expect(runVisualConfirmationFixture(hud, imu)).resolves.toBe(true);
    expect(imu.prepare).toHaveBeenCalledWith('visual-confirmation', expect.any(Function));
    expect(imu.activate).toHaveBeenCalledWith('visual-confirmation', 8000);

    settle?.(true);
    expect(hud.dismissConfirmation).toHaveBeenCalledWith('visual-confirmation');
  });
});
