import type { ProductivityHudModel, UplinkMessage } from './types';

type ProductivityViewAck = Extract<UplinkMessage, { kind: 'productivity.view.ack' }>;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Build the simulator receipt only after the current client retained a
 * gateway-bound verifying surface. Its fields are opaque, short-lived routing
 * data—not Calendar or Reminder content and not a claim about native banners
 * or physical G2 rasterization.
 */
export function productivityViewAckFor(
  productivity: ProductivityHudModel,
): ProductivityViewAck | undefined {
  const presentationReceiptId = productivity.presentationReceiptId;
  const generation = productivity.presentationReceiptGeneration;
  if (
    productivity.phase !== 'verifying'
    || typeof presentationReceiptId !== 'string'
    || !UUID.test(presentationReceiptId)
    || typeof generation !== 'number'
    || !Number.isSafeInteger(generation)
    || generation <= 0
  ) return undefined;
  return {
    kind: 'productivity.view.ack',
    presentationReceiptId,
    generation,
    outcome: 'accepted',
  };
}
