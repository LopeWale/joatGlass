import { describe, expect, it } from 'vitest';
import { GesturePolicy } from './GesturePolicy';
import {
  NOTIFICATION_DISMISS_HINT,
  PANEL_OVERVIEW_HINT,
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
      .toBe('tap: dismiss · 2x tap: menu');
    // Case and spacing variants are all authored somewhere in this repo.
    expect(GesturePolicy.formatHint('SWIPE move · TAP open · LEFT x2 back'))
      .toBe('TAP open · SWIPE move · 2x tap back');
    expect(GesturePolicy.formatHint('left×2 overview')).toBe('2x tap overview');
  });

  it('formatHint speaks one template: tap, swipe, long tap, 2x tap, in that order', () => {
    // The wearer's ruling (2026-10-07): "'tap', 'long tap' and '2x tap' as
    // a template so it can be the same on the pages that need it".
    expect(GesturePolicy.formatHint('down more · left x2: back'))
      .toBe('swipe: more · 2x tap: back');
    expect(GesturePolicy.formatHint('up previous · down more · left x2: back'))
      .toBe('swipe: page · 2x tap: back');
    expect(GesturePolicy.formatHint('up previous · left x2: back'))
      .toBe('swipe: previous · 2x tap: back');
    expect(GesturePolicy.formatHint('tap: open · hold: refresh · double-click: back'))
      .toBe('tap: open · long tap: refresh · 2x tap: back');
    expect(GesturePolicy.formatHint('double-click: menu · long-press: mic'))
      .toBe('long tap: mic · 2x tap: menu');
    expect(GesturePolicy.formatHint('Continue conversation? tap stays · x2 exits'))
      .toBe('Continue conversation? tap stays · 2x tap exits');
    // Non-gestures (a page count, a spoken command) keep their places.
    expect(GesturePolicy.formatHint('down older · tap read · say reply · left x2: back'))
      .toBe('tap read · swipe: older · say reply · 2x tap: back');
    expect(GesturePolicy.formatHint('1/2 · swipe: page · tap: dismiss'))
      .toBe('1/2 · tap: dismiss · swipe: page');
    expect(GesturePolicy.formatHint('Back: double-click to Planning · auto in 5s'))
      .toBe('2x tap: Planning · auto in 5s');
    for (const hint of ['hold: mic', 'down more', 'up previous · down more', 'long-press: mic', 'double-click: back']) {
      expect(GesturePolicy.formatHint(hint)).not.toMatch(/\b(hold|up previous|down more|long-press|double-click)\b/);
    }
  });

  it('formatHint output is free of hardware names', () => {
    for (const hint of [
      NOTIFICATION_DISMISS_HINT,
      PANEL_OVERVIEW_HINT,
      'left x2: back',
      'tap: retry · left x2: back',
      'left x2 overview',
      'left x2 Planning',
      'tap: choose · swipe: move · left x2: back',
    ]) {
      expect(GesturePolicy.formatHint(hint)).not.toMatch(NAMES_HARDWARE);
    }
  });

});
