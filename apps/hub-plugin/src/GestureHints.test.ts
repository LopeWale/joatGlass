import { describe, expect, it } from 'vitest';
import { GesturePolicy } from './GesturePolicy';
import {
  NOTIFICATION_DISMISS_HINT,
  PANEL_OVERVIEW_HINT,
  ASSISTANT_OVERLAY_DISMISS_HINT,
  ASSISTANT_OVERLAY_PAGE_HINT,
  brainstormActionHint,
} from './HudCanvasRenderer';

/**
 * The wearer uses an R1 ring AND the glasses, interchangeably — "I should be
 * able to use anything I want, ring or glasses". The input path already
 * honours that: EventRouter.isLocalTapSource admits TOUCH_EVENT_FROM_RING,
 * _FROM_GLASSES_L and _FROM_GLASSES_R alike.
 *
 * What the wearer SEES has to honour it too. A hint reading "left x2" names a
 * temple a ring user never presses, and one reading "right/ring" names
 * hardware a left-temple user is not holding. Even's own gesture grammar
 * names the ACTION and never the hardware — tap, double-tap, swipe,
 * long-press (evenrealities.com/blogs/even-insider/the-grammar-of-a-ring,
 * and the support page "How to Control").
 *
 * Hints are still AUTHORED in the legacy "left x2" form and rewritten by
 * GesturePolicy.formatHint at render. That is one forgotten call away from
 * leaking, and it did leak: the notifications footer substituted its constant
 * after render()'s normalisation and drew "left x2: menu" verbatim (dd63111).
 * This is the guard against the next one.
 */
const NAMES_HARDWARE = /\b(left|right|temple|ring)\b/i;

describe('wearer-facing hints never name a device or a side', () => {
  it('formatHint strips the authored left-temple form', () => {
    expect(GesturePolicy.formatHint('tap: dismiss · left x2: menu'))
      .toBe('tap: dismiss · double-click: menu');
    // Case and spacing variants are all authored somewhere in this repo.
    expect(GesturePolicy.formatHint('SWIPE move · TAP open · LEFT x2 back'))
      .toBe('SWIPE move · TAP open · double-click back');
    expect(GesturePolicy.formatHint('left×2 overview')).toBe('double-click overview');
  });

  it('formatHint speaks one vocabulary: tap, double-click, swipe, long-press', () => {
    // The reader (Mac reader_hint), the list beside it, and Home used to
    // disagree: "down more" vs "hold: refresh" vs "long-press: mic".
    expect(GesturePolicy.formatHint('down more · left x2: back'))
      .toBe('swipe: more · double-click: back');
    expect(GesturePolicy.formatHint('up previous · down more · left x2: back'))
      .toBe('swipe: page · double-click: back');
    expect(GesturePolicy.formatHint('up previous · left x2: back'))
      .toBe('swipe: previous · double-click: back');
    expect(GesturePolicy.formatHint('tap: open · hold: refresh · double-click: back'))
      .toBe('tap: open · long-press: refresh · double-click: back');
    expect(GesturePolicy.formatHint('Continue conversation? tap stays · x2 exits'))
      .toBe('Continue conversation? tap stays · double-click exits');
    expect(GesturePolicy.formatHint('down older · tap read · say reply · left x2: back'))
      .toBe('swipe: older · tap read · say reply · double-click: back');
    for (const hint of ['hold: mic', 'down more', 'up previous · down more']) {
      expect(GesturePolicy.formatHint(hint)).not.toMatch(/\b(hold|up previous|down more)\b/);
    }
  });

  it('formatHint output is free of hardware names', () => {
    for (const hint of [
      NOTIFICATION_DISMISS_HINT,
      PANEL_OVERVIEW_HINT,
      ASSISTANT_OVERLAY_DISMISS_HINT,
      ASSISTANT_OVERLAY_PAGE_HINT,
      'left x2: back',
      'tap: retry · left x2: back',
      'left x2 overview',
      'left x2 Planning',
      'tap: choose · swipe: move · left x2: back',
    ]) {
      expect(GesturePolicy.formatHint(hint)).not.toMatch(NAMES_HARDWARE);
    }
  });

  it('every brainstorm action hint is already device-neutral', () => {
    const screens = ['brainstorm', 'agent_terminal'] as const;
    const states = [
      'running', 'acceptance_ready', 'topic_capture', 'ready', 'idle',
    ] as const;
    for (const screen of screens) {
      for (const state of states) {
        const hint = brainstormActionHint({
          screen, state, noToolsChecked: false, acceptanceArmed: false,
          approvals: [], inputRequired: false, taskRecovery: undefined,
        } as unknown as Parameters<typeof brainstormActionHint>[0]);
        expect(GesturePolicy.formatHint(hint)).not.toMatch(NAMES_HARDWARE);
      }
    }
  });
});
