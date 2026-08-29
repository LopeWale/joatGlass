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

  it('forwards the privacy-bucketed location probe shape without expanding it', () => {
    const sent: UplinkMessage[] = [];
    const trace = new MacG2DiagnosticTrace({ sendDiagnostic: (message) => sent.push(message) });
    trace.setEnabled(true);

    trace.record({
      type: 'location_probe',
      sampleSequence: 3,
      profile: 'high',
      lifecycle: 'foreground',
      fields: 5,
      cadence: '1_5s',
      accuracy: 'lte25',
      timestampAge: 'fresh',
      displacement: '20_100',
      headingDelta: '31_90',
    });

    expect(sent).toEqual([{
      kind: 'g2.diagnostic',
      event: {
        type: 'location_probe',
        sampleSequence: 3,
        profile: 'high',
        lifecycle: 'foreground',
        fields: 5,
        cadence: '1_5s',
        accuracy: 'lte25',
        timestampAge: 'fresh',
        displacement: '20_100',
        headingDelta: '31_90',
      },
    }]);
  });

  it('forwards a first-light outcome without HUD content or pixels', () => {
    const sent: UplinkMessage[] = [];
    const trace = new MacG2DiagnosticTrace({ sendDiagnostic: (message) => sent.push(message) });
    trace.setEnabled(true);

    trace.record({
      type: 'hud_surface',
      stage: 'visual_rebuild',
      surface: 'visual',
      outcome: 'failed',
    });

    expect(sent).toEqual([{
      kind: 'g2.diagnostic',
      event: {
        type: 'hud_surface',
        stage: 'visual_rebuild',
        surface: 'visual',
        outcome: 'failed',
      },
    }]);
  });

  it('forwards only the fixed Brainstorm action and completed-frame receipt', () => {
    const sent: UplinkMessage[] = [];
    const trace = new MacG2DiagnosticTrace({ sendDiagnostic: (message) => sent.push(message) });
    trace.setEnabled(true);

    trace.record({
      type: 'brainstorm_action', action: 'show_terminal', surface: 'brainstorm',
      source: 'right', gesture: 'tap', delivery: 'sys',
    });
    trace.record({
      type: 'visual_frame', kind: 'brainstorm', surface: 'agent_terminal', state: 'running',
      sha256: 'a'.repeat(64),
    });

    expect(sent).toEqual([{
      kind: 'g2.diagnostic',
      event: {
        type: 'brainstorm_action', action: 'show_terminal', surface: 'brainstorm',
        source: 'right', gesture: 'tap', delivery: 'sys',
      },
    }, {
      kind: 'g2.diagnostic',
      event: {
        type: 'visual_frame', kind: 'brainstorm', surface: 'agent_terminal', state: 'running',
        sha256: 'a'.repeat(64),
      },
    }]);
  });
});
