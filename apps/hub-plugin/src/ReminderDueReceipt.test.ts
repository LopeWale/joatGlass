import { describe, expect, it } from 'vitest';
import { reminderDueAckFor, reminderDueCompleteFor } from './ReminderDueReceipt';

const due = {
  domain: 'reminders' as const,
  phase: 'due' as const,
  title: 'Private reminder',
  primary: 'Due now',
  secondary: 'Still incomplete',
  hint: '2x tap: Planning',
  reminderDueId: '67ec97c5-a0fd-48ec-89cb-1b756ddaf87a',
  reminderDueAttemptId: 'a76b12f1-45e4-41fd-aed8-086f6b6b1954',
  reminderDueGeneration: 7,
};

describe('reminder due receipt', () => {
  it('acknowledges only a generation-bound opaque due view', () => {
    expect(reminderDueAckFor(due)).toEqual({
      kind: 'reminder_due.ack',
      reminderDueId: '67ec97c5-a0fd-48ec-89cb-1b756ddaf87a',
      reminderDueAttemptId: 'a76b12f1-45e4-41fd-aed8-086f6b6b1954',
      generation: 7,
      outcome: 'accepted',
    });
  });

  it.each([
    { ...due, domain: 'calendar' as const },
    { ...due, phase: 'saved' as const },
    { ...due, reminderDueId: 'private title' },
    { ...due, reminderDueAttemptId: 'prior attempt' },
    { ...due, reminderDueGeneration: 0 },
    { ...due, reminderDueGeneration: Number.MAX_SAFE_INTEGER + 1 },
  ])('refuses an unbound or non-due receipt request', (productivity) => {
    expect(reminderDueAckFor(productivity)).toBeUndefined();
  });
});

describe('reminder due completion', () => {
  it('carries exactly the binding the acknowledgement carried', () => {
    expect(reminderDueCompleteFor(due)).toEqual({
      kind: 'reminder_due.complete',
      reminderDueId: '67ec97c5-a0fd-48ec-89cb-1b756ddaf87a',
      reminderDueAttemptId: 'a76b12f1-45e4-41fd-aed8-086f6b6b1954',
      generation: 7,
    });
    expect(reminderDueCompleteFor({ ...due, phase: 'failed' })).toBeUndefined();
    expect(reminderDueCompleteFor({ ...due, reminderDueGeneration: 0 })).toBeUndefined();
  });
});
