import type { ProductivityHudModel, UplinkMessage } from './types';

type ReminderDueAck = Extract<UplinkMessage, { kind: 'reminder_due.ack' }>;

const OCCURRENCE_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Build the plugin-acceptance receipt only for a gateway-bound due view.
 *
 * Its fields are opaque local/gateway UUIDs and a Mac connection generation;
 * none can contain Calendar or Reminder content. This receipt identifies an
 * admitted current Hub client, not native notification delivery, G2 pixels,
 * or wearer attention.
 */
export function reminderDueAckFor(
  productivity: ProductivityHudModel,
): ReminderDueAck | undefined {
  const reminderDueId = productivity.reminderDueId;
  const reminderDueAttemptId = productivity.reminderDueAttemptId;
  const generation = productivity.reminderDueGeneration;
  if (
    productivity.domain !== 'reminders'
    || productivity.phase !== 'due'
    || typeof reminderDueId !== 'string'
    || !OCCURRENCE_UUID.test(reminderDueId)
    || typeof reminderDueAttemptId !== 'string'
    || !OCCURRENCE_UUID.test(reminderDueAttemptId)
    || typeof generation !== 'number'
    || !Number.isSafeInteger(generation)
    || generation <= 0
  ) return undefined;
  return {
    kind: 'reminder_due.ack',
    reminderDueId,
    reminderDueAttemptId,
    generation,
    outcome: 'accepted',
  };
}
