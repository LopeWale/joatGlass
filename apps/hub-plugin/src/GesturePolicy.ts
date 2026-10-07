import { OsEventTypeList, EventSourceType, type EvenHubEvent } from '@evenrealities/even_hub_sdk';

/** The physical G2 input a system envelope names. Back means the same on
 * every one of them; the name survives only so an explicitly armed
 * diagnostic can prove which input started it (BrainstormInputOwner). */
export type GestureSource = 'left' | 'right' | 'ring';

/** Product meaning belongs here, independently of the SDK envelope or hand.
 * Lifecycle events and native contextual menus retain host ownership. */
export class GesturePolicy {
  resolve(event: EvenHubEvent): {
    intent: 'back'; delivery: 'sys' | 'text' | 'list'; source: GestureSource | undefined;
  } | undefined {
    // A system envelope has priority, especially suspend/exit lifecycle.
    if (event.sysEvent) {
      return event.sysEvent.eventType === OsEventTypeList.DOUBLE_CLICK_EVENT
        ? {
          intent: 'back', delivery: 'sys',
          source: GesturePolicy.sourceOf(event.sysEvent.eventSource),
        } : undefined;
    }
    const envelopes = [
      ['text', event.textEvent], ['list', event.listEvent],
    ] as const;
    for (const [delivery, input] of envelopes) {
      if (input?.eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
        return { intent: 'back', delivery, source: undefined };
      }
    }
    return undefined;
  }

  /** Dummy/absent sources stay anonymous: they are real local gestures for
   * Back, but can never claim to be a named temple or the ring. */
  static sourceOf(source: number | undefined): GestureSource | undefined {
    switch (source) {
      case EventSourceType.TOUCH_EVENT_FROM_GLASSES_L: return 'left';
      case EventSourceType.TOUCH_EVENT_FROM_GLASSES_R: return 'right';
      case EventSourceType.TOUCH_EVENT_FROM_RING: return 'ring';
      default: return undefined;
    }
  }

  static backHint = '2x tap';

  /** The template's gestures, in the order every page names them. */
  static readonly GESTURE_ORDER = ['tap', 'swipe', 'long tap', '2x tap'] as const;

  /**
   * One gesture template on every page, whatever a Mac version or an older
   * screen authored (the wearer's ruling, 2026-10-07: "'tap', 'long tap'
   * and '2x tap' as a template so it can be the same on the pages that
   * need it"). The words: tap, swipe, long tap, 2x tap — never
   * double-click, long-press, hold or left x2. The order: tap · swipe ·
   * long tap · 2x tap; anything that is not a gesture (a page count, a
   * spoken command) keeps its place.
   * Authored forms still in the repo or on an older Mac, and what they become:
   *   double-click / double tap / left x2 / x2 → 2x tap
   *   long-press / hold                         → long tap
   *   Back: double-click to Planning            → 2x tap: Planning
   *   up previous · down more → swipe: page; down more → swipe: more;
   *   up previous → swipe: previous; down older → swipe: older
   */
  static formatHint(hint: string): string {
    const words = hint
      .replace(/\bBack:\s*(?:double[- ]?(?:click|tap)|left\s*(?:x|×)\s*2)\s+to\s+([^·]+?)\s*(?=·|$)/giu, '2x tap: $1')
      .replace(/\bBack:\s*(?:double[- ]?(?:click|tap)|left\s*(?:x|×)\s*2)/giu, '2x tap: back')
      .replace(/left\s*(?:x|×)\s*2\s+(?=[A-Z])/gu, '2x tap: ')
      .replace(/left\s*(?:x|×)\s*2/giu, '2x tap')
      .replace(/(?<![\w-])(?:x|×)\s*2(?![\w])/giu, '2x tap')
      .replace(/double[- ]?(?:click|tap)/giu, '2x tap')
      .replace(/long[- ]?press/giu, 'long tap')
      .replace(/\bhold\b/giu, 'long tap')
      .replace(/\bup previous\s*·\s*down more\b/giu, 'swipe: page')
      .replace(/\bdown more\b/giu, 'swipe: more')
      .replace(/\bup previous\b/giu, 'swipe: previous')
      .replace(/\bdown older\b/giu, 'swipe: older');
    const segments = words.split(/\s*·\s*/u);
    const rank = (segment: string): number => {
      const gesture = /^(2x tap|long tap|tap|swipe)\b/iu.exec(segment)?.[1]?.toLowerCase();
      return gesture ? GesturePolicy.GESTURE_ORDER.indexOf(gesture as typeof GesturePolicy.GESTURE_ORDER[number]) : -1;
    };
    // The gestures, in the template's order, in the places gestures held.
    const gestures = segments.filter((segment) => rank(segment) >= 0)
      .map((segment, index) => ({ segment, index }))
      .sort((a, b) => rank(a.segment) - rank(b.segment) || a.index - b.index)
      .map(({ segment }) => segment);
    let next = 0;
    return segments.map((segment) => (rank(segment) >= 0 ? gestures[next++]! : segment)).join(' · ');
  }
}
