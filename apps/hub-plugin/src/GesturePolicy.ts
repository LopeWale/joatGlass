import { OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';

/** Product meaning belongs here, independently of the SDK envelope or hand.
 * Lifecycle events and native contextual menus retain host ownership. */
export class GesturePolicy {
  resolve(event: EvenHubEvent): {
    intent: 'back'; delivery: 'sys' | 'text' | 'list';
  } | undefined {
    // A system envelope has priority, especially suspend/exit lifecycle.
    if (event.sysEvent) {
      return event.sysEvent.eventType === OsEventTypeList.DOUBLE_CLICK_EVENT
        ? { intent: 'back', delivery: 'sys' } : undefined;
    }
    const envelopes = [
      ['text', event.textEvent], ['list', event.listEvent],
    ] as const;
    for (const [delivery, input] of envelopes) {
      if (input?.eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
        return { intent: 'back', delivery };
      }
    }
    return undefined;
  }

  static backHint = 'double-click';

  /** One gesture vocabulary on every page, whatever a Mac version or an
   * older screen authored. Even names the ACTION, never the hardware —
   * tap, double-tap, swipe, long-press ("The Grammar of a Ring"; support
   * "How to Control") — and the wearer uses a ring or either temple.
   * Authored forms still in the repo and their wearer-facing spelling:
   *   left x2 / x2        → double-click   (the wearer's accepted spelling)
   *   hold                → long-press
   *   up previous · down more → swipe: page; down more → swipe: more;
   *   up previous → swipe: previous; down older → swipe: older
   * The reader hint used to read "down more" while the list beside it read
   * "hold: refresh" and Home read "long-press: mic" (2026-09-22). */
  static formatHint(hint: string): string {
    return hint
      .replace(/left\s*(?:x|×)\s*2/gi, GesturePolicy.backHint)
      .replace(/(?<![\w-])(?:x|×)\s*2(?![\w])/gi, GesturePolicy.backHint)
      .replace(/\bhold\b/gi, 'long-press')
      .replace(/\bup previous\s*·\s*down more\b/gi, 'swipe: page')
      .replace(/\bdown more\b/gi, 'swipe: more')
      .replace(/\bup previous\b/gi, 'swipe: previous')
      .replace(/\bdown older\b/gi, 'swipe: older');
  }
}
