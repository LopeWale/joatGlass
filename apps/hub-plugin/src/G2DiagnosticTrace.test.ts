import { describe, expect, it } from 'vitest';
import { MacG2DiagnosticTrace } from './G2DiagnosticTrace';
import type { UplinkMessage } from './types';

describe('MacG2DiagnosticTrace', () => {
  it('is inert until the Mac explicitly enables the capture', () => {
    const sent: UplinkMessage[] = [];
    const trace = new MacG2DiagnosticTrace({ sendDiagnostic: (message) => sent.push(message) });

    trace.record({
      type: 'gesture', gesture: 'long_press', delivery: 'sys',
    });
    expect(sent).toEqual([]);

    trace.setEnabled(true);
    trace.record({
      type: 'gesture', gesture: 'long_press', delivery: 'sys',
    });
    expect(sent).toEqual([{
      kind: 'g2.diagnostic',
      event: { type: 'gesture', gesture: 'long_press', delivery: 'sys' },
    }]);

    trace.setEnabled(false);
    trace.record({
      type: 'gesture', gesture: 'tap', delivery: 'sys',
    });
    expect(sent).toHaveLength(1);
  });

  it('never lets an unavailable link interrupt the HUD path', () => {
    const trace = new MacG2DiagnosticTrace({
      sendDiagnostic: () => { throw new Error('socket closed'); },
    });
    trace.setEnabled(true);

    expect(() => trace.record({
      type: 'tile_upload', frame: 1, order: 0, containerId: 4, outcome: 'success',
    })).not.toThrow();
  });
});
