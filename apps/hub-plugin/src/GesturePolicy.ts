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

  static backHint = 'double-click';

  /** Display compatibility for Mac versions with the old left-only hints. */
  static formatHint(hint: string): string {
    return hint.replace(/left\s*(?:x|×)\s*2/gi, GesturePolicy.backHint);
  }
}
