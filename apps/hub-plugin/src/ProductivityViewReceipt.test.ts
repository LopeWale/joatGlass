import { describe, expect, it } from 'vitest';
import { productivityViewAckFor } from './ProductivityViewReceipt';

const verifying = {
  domain: 'calendar' as const,
  phase: 'verifying' as const,
  title: 'Private calendar title',
  primary: 'Saving calendar event',
  secondary: 'Checking native read-back',
  hint: 'Please wait',
  presentationReceiptId: 'a76b12f1-45e4-41fd-aed8-086f6b6b1954',
  presentationReceiptGeneration: 7,
};

describe('productivity verifying presentation receipt', () => {
  it('acknowledges only the opaque, generation-bound verifying surface', () => {
    expect(productivityViewAckFor(verifying)).toEqual({
      kind: 'productivity.view.ack',
      presentationReceiptId: 'a76b12f1-45e4-41fd-aed8-086f6b6b1954',
      generation: 7,
      outcome: 'accepted',
    });
  });

  it.each([
    { ...verifying, phase: 'saved' as const },
    { ...verifying, presentationReceiptId: 'private calendar title' },
    { ...verifying, presentationReceiptGeneration: 0 },
    { ...verifying, presentationReceiptGeneration: Number.MAX_SAFE_INTEGER + 1 },
  ])('refuses a non-verifying or unbound presentation receipt', (productivity) => {
    expect(productivityViewAckFor(productivity)).toBeUndefined();
  });
});
