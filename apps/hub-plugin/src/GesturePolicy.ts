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

  /** Display compatibility for Mac versions with the old left-only hints. */
  static formatHint(hint: string): string {
    return hint.replace(/left\s*(?:x|×)\s*2/gi, GesturePolicy.backHint);
  }
}
