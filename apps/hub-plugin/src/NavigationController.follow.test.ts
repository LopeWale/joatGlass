import { describe, expect, it } from 'vitest';
import { NavigationController, nearestMapPanel } from './NavigationController';
import type { NavigationHudModel, NavigationMapPanelModel } from './NavigationHud';
import type { NavigationRoutePlan } from './NavigationProgress';
import type { UplinkMessage } from './types';

/**
 * The guidance page follows the wearer. Until 2026-10-05 it repainted only
 * when the instruction or the rounded distance changed, so over a kilometre
 * the position dot, the progress bar and the ETA froze for a hundred
 * metres at a time; the street tile was fetched once at the origin and
 * never again; and a fix the host reported without an accuracy figure, or
 * with a timestamp in seconds, was rejected as "uncertain" for the whole
 * walk. These pin the new behaviour on the controller's own seams.
 */

/** A straight 3 km route east along the equator: a turn at 2 km, the end at 3 km. */
const plan: NavigationRoutePlan = {
  mode: 'walking',
  geometry: [
    { latitude: 0, longitude: 0 },
    { latitude: 0, longitude: 0.009 },   // ~1000 m
    { latitude: 0, longitude: 0.018 },   // ~2000 m
    { latitude: 0, longitude: 0.027 },   // ~3000 m
  ],
  steps: [
    { id: 's1', maneuver: 'turn_right', instruction: 'onto Test Ave', distanceAlongRouteM: 2000 },
    { id: 's2', maneuver: 'arrive', instruction: 'Cajun Corner', distanceAlongRouteM: 3000 },
  ],
  policy: {
    maxAccuracyM: 25, staleAfterMs: 5_000, corridorRadiusM: 25, ambiguityMarginM: 6,
    ambiguityProgressDeltaM: 80, searchBackM: 100, searchForwardM: 400,
    minCourseDisplacementM: 5, wrongWayAngleDeg: 120, wrongWayMinBacktrackM: 12,
    wrongWaySamples: 3, offRouteSamples: 3, arrivalRadiusM: 15, arrivalSamples: 2,
    uTurnSuppressionM: 40,
  },
};

const METRE_DEG = 0.000009; // one metre of longitude at the equator

function harness(clockStart = 1_700_000_000_000) {
  let now = clockStart;
  let consumer: ((fix: object, receivedAt: number) => void) | undefined;
  const frames: NavigationHudModel[] = [];
  const sent: UplinkMessage[] = [];
  const controller = new NavigationController(
    { showNavigation: async (frame) => { frames.push(frame); return true; } },
    { sendNavigation: (message) => { sent.push(message); } },
    {
      setNavigationConsumer: async (next) => {
        consumer = next as typeof consumer;
        return true;
      },
      requestNavigationOrigin: async () => null,
    },
    undefined,
    () => now,
  );
  /** A fix `metres` along the route, `secondsLater` after the clock start. */
  const walk = async (metres: number, secondsLater: number, extra: object = {}) => {
    now = clockStart + secondsLater * 1000;
    consumer?.({ latitude: 0, longitude: metres * METRE_DEG, accuracy: 5, timestamp: now - 100, ...extra }, now);
    await controller.whenIdle();
  };
  const install = () => controller.installRoute({
    kind: 'navigation.route', sessionId: 'nav-1', requestId: 'route-request-1',
    generation: 10, routeId: 'route-1', revision: 1,
    destinationLabel: 'Cajun Corner', etaMinutes: 24, plan,
  });
  const states = () => sent.filter((message) => message.kind === 'navigation.state');
  return { controller, frames, sent, walk, install, states, tick: (seconds: number) => { now = clockStart + seconds * 1000; } };
}

describe('the guidance page follows the wearer', () => {
  it('repaints when the wearer has moved, at most once a second, without telling the Mac', async () => {
    const h = harness();
    expect(await h.install()).toBe(true);
    await h.walk(100, 0);
    expect(h.frames).toHaveLength(1);
    expect(h.frames[0]).toMatchObject({ view: 'guidance', maneuverDistanceM: 1900 });
    expect(h.states()).toHaveLength(1);

    // Ten metres further, one second later, same instruction, same rounded
    // distance ("1.9 km" both times): the page repaints, the Mac hears
    // nothing new.
    await h.walk(110, 1);
    expect(h.frames).toHaveLength(2);
    expect(h.frames[1]?.map?.position?.longitude).toBeCloseTo(110 * METRE_DEG, 9);
    expect(h.states()).toHaveLength(1);

    // Half a second later: moved, but inside the pacing window — held.
    await h.walk(115, 1.5);
    expect(h.frames).toHaveLength(2);
    // A second later it goes out, with the newest position.
    await h.walk(125, 2.5);
    expect(h.frames).toHaveLength(3);
    expect(h.frames[2]?.map?.position?.longitude).toBeCloseTo(125 * METRE_DEG, 9);

    // Standing still (under three metres): nothing to show.
    await h.walk(126, 4);
    expect(h.frames).toHaveLength(3);

    // An instruction change always paints, pacing or not: walk up to and
    // past the turn (within the tracker's 400 m search window per fix).
    const statesBefore = h.states().length;
    for (const [metres, seconds] of [[450, 5], [800, 6], [1_150, 7], [1_500, 8], [1_850, 9], [2_010, 9.2]] as const) {
      await h.walk(metres, seconds);
    }
    expect(h.frames[h.frames.length - 1]).toMatchObject({ view: 'guidance', maneuver: 'arrive' });
    // Each rounded-distance step on the way was a state the Mac heard
    // about; the position-only repaints before it were not.
    expect(h.states().length).toBeGreaterThan(statesBefore);
    expect(h.states().length).toBeLessThan(h.frames.length);
    expect(JSON.stringify(h.sent)).not.toContain('"latitude"');
  });

  it('shows a street tile the moment it arrives, and the one nearest the wearer', async () => {
    const h = harness();
    await h.install();
    await h.walk(100, 0);
    expect(h.frames[0]?.map?.panel).toBeUndefined();

    const tile = (longitude: number): NavigationMapPanelModel => ({
      latitude: 0, longitude, zoom: 16, width: 284, height: 220, image: { longitude } as never,
    });
    const decode = async (source: string) => ({ source } as never);
    for (const longitude of [0, 0.0045, 0.009]) {
      await h.controller.installMapPanel({
        kind: 'navigation.map', sessionId: 'nav-1', latitude: 0, longitude,
        zoom: 16, width: 284, height: 220, png: 'x',
      }, decode);
    }
    await h.controller.whenIdle();
    // The arrival of a tile repainted the page it was for.
    expect(h.frames).toHaveLength(4);
    expect(h.frames[3]?.map?.panel?.longitude).toBe(0);

    // Walking on (within the tracker's 400 m search window per fix), the
    // pane switches to the tile nearest the wearer.
    await h.walk(350, 2);
    await h.walk(600, 4);
    expect(h.frames[h.frames.length - 1]).toMatchObject({ view: 'guidance' });
    expect(h.frames[h.frames.length - 1]?.map?.panel?.longitude).toBe(0.0045);
    await h.walk(850, 6);
    expect(h.frames[h.frames.length - 1]?.map?.panel?.longitude).toBe(0.009);

    expect(nearestMapPanel([tile(0), tile(0.009)], { latitude: 0, longitude: 0.004 })?.longitude).toBe(0);
    expect(nearestMapPanel([tile(0), tile(0.009)], { latitude: 0, longitude: 0.005 })?.longitude).toBe(0.009);
    expect(nearestMapPanel([], { latitude: 0, longitude: 0 })).toBeUndefined();
  });

  it('accepts a fix without an accuracy figure and a timestamp in seconds', async () => {
    const h = harness();
    await h.install();
    const now = 1_700_000_000_000;
    // No accuracy: trusted at the policy's limit, not rejected.
    await h.walk(100, 0, { accuracy: undefined });
    expect(h.frames[0]).toMatchObject({ view: 'guidance' });
    // Seconds, as some hosts report: read as seconds, so the fix is fresh.
    await h.walk(140, 1, { timestamp: (now + 1000) / 1000 - 0.1 });
    expect(h.frames[1]).toMatchObject({ view: 'guidance' });
    // Genuinely stale still says so.
    await h.walk(180, 2, { timestamp: now - 60_000 });
    expect(h.frames[2]).toMatchObject({ view: 'uncertain', secondary: 'Route match is uncertain' });
  });
});
