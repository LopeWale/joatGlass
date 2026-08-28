import { describe, expect, it } from 'vitest';

import {
  NavigationProgressTracker,
  type NavigationProgressPolicy,
  type NavigationRoutePlan,
} from './NavigationProgress';

const METERS_PER_DEGREE = 111_195;

function point(xM: number, yM = 0) {
  return { latitude: yM / METERS_PER_DEGREE, longitude: xM / METERS_PER_DEGREE };
}

const policy: NavigationProgressPolicy = {
  maxAccuracyM: 20,
  staleAfterMs: 5_000,
  corridorRadiusM: 30,
  ambiguityMarginM: 5,
  ambiguityProgressDeltaM: 100,
  searchBackM: 120,
  searchForwardM: 400,
  minCourseDisplacementM: 5,
  wrongWayAngleDeg: 120,
  wrongWayMinBacktrackM: 15,
  wrongWaySamples: 2,
  offRouteSamples: 3,
  arrivalRadiusM: 15,
  arrivalSamples: 2,
  uTurnSuppressionM: 50,
};

function route(overrides: Partial<NavigationRoutePlan> = {}): NavigationRoutePlan {
  return {
    mode: 'walking',
    geometry: [point(0), point(1_000)],
    steps: [
      { id: 'depart', maneuver: 'depart', instruction: 'Continue east', distanceAlongRouteM: 0 },
      { id: 'right', maneuver: 'turn_right', instruction: 'Turn right', distanceAlongRouteM: 500 },
      { id: 'arrive', maneuver: 'arrive', instruction: 'Arrive', distanceAlongRouteM: 1_000 },
    ],
    policy,
    ...overrides,
  };
}

function fix(xM: number, yM = 0, at = 10_000, accuracy = 5) {
  return { ...point(xM, yM), accuracy, timestamp: at };
}

describe('NavigationProgressTracker', () => {
  it('selects cues by along-route progress rather than radius-only step skipping', () => {
    const tracker = new NavigationProgressTracker(route());
    const state = tracker.accept(fix(100), 10_100);
    expect(state).toMatchObject({
      kind: 'guidance',
      maneuver: 'turn_right',
      instruction: 'Turn right',
      maneuverDistanceM: 400,
      remainingDistanceM: 900,
    });
  });

  it('holds the route as uncertain for poor or stale fixes', () => {
    const poor = new NavigationProgressTracker(route()).accept(fix(100, 0, 10_000, 45), 10_100);
    expect(poor).toMatchObject({ kind: 'uncertain', reason: 'accuracy' });
    const stale = new NavigationProgressTracker(route()).accept(fix(100, 0, 1_000), 10_100);
    expect(stale).toMatchObject({ kind: 'uncertain', reason: 'stale' });
  });

  it('debounces off-route evidence instead of rerouting on one noisy fix', () => {
    const tracker = new NavigationProgressTracker(route());
    expect(tracker.accept(fix(100, 100, 10_000), 10_100)).toMatchObject({
      kind: 'uncertain', reason: 'off_route_pending',
    });
    expect(tracker.accept(fix(110, 100, 11_000), 11_100).kind).toBe('uncertain');
    expect(tracker.accept(fix(120, 100, 12_000), 12_100)).toMatchObject({ kind: 'off_route' });
  });

  it('requires sustained reverse movement and never accuses stationary jitter', () => {
    const tracker = new NavigationProgressTracker(route());
    tracker.accept(fix(100, 0, 8_000), 8_100);
    tracker.accept(fix(500, 0, 9_000), 9_100);
    tracker.accept(fix(600, 0, 10_000), 10_100);
    expect(tracker.accept(fix(602, 0, 11_000), 11_100).kind).toBe('guidance');
    expect(tracker.accept(fix(599, 0, 12_000), 12_100).kind).toBe('guidance');
    expect(tracker.accept(fix(575, 0, 13_000), 13_100).kind).toBe('guidance');
    expect(tracker.accept(fix(545, 0, 14_000), 14_100)).toMatchObject({ kind: 'wrong_way' });
  });

  it('suppresses wrong-way warnings near a prescribed U-turn', () => {
    const tracker = new NavigationProgressTracker(route({
      steps: [
        { id: 'uturn', maneuver: 'u_turn', instruction: 'Make a U-turn', distanceAlongRouteM: 600 },
        { id: 'arrive', maneuver: 'arrive', instruction: 'Arrive', distanceAlongRouteM: 1_000 },
      ],
    }));
    tracker.accept(fix(100, 0, 8_000), 8_100);
    tracker.accept(fix(500, 0, 9_000), 9_100);
    tracker.accept(fix(630, 0, 10_000), 10_100);
    tracker.accept(fix(605, 0, 11_000), 11_100);
    expect(tracker.accept(fix(575, 0, 12_000), 12_100).kind).toBe('guidance');
  });

  it('marks parallel-route matches uncertain rather than jumping progress', () => {
    const tracker = new NavigationProgressTracker(route({
      geometry: [point(0, 0), point(1_000, 0), point(1_000, 20), point(0, 20)],
    }));
    expect(tracker.accept(fix(500, 10), 10_100)).toMatchObject({
      kind: 'uncertain', reason: 'ambiguous',
    });
  });

  it('requires stable arrival evidence', () => {
    const tracker = new NavigationProgressTracker(route());
    tracker.accept(fix(100, 0, 7_000), 7_100);
    tracker.accept(fix(500, 0, 8_000), 8_100);
    tracker.accept(fix(900, 0, 9_000), 9_100);
    expect(tracker.accept(fix(992, 0, 10_000), 10_100).kind).toBe('guidance');
    expect(tracker.accept(fix(997, 0, 11_000), 11_100)).toMatchObject({ kind: 'arrived' });
  });

  it('requires endpoint distance instead of arriving beside the destination', () => {
    const tracker = new NavigationProgressTracker(route({
      policy: { ...policy, maxAccuracyM: 30 },
    }));
    tracker.accept(fix(100, 0, 7_000), 7_100);
    tracker.accept(fix(500, 0, 8_000), 8_100);
    tracker.accept(fix(900, 0, 9_000), 9_100);
    expect(tracker.accept(fix(997, 40, 10_000, 25), 10_100).kind).not.toBe('arrived');
    expect(tracker.accept(fix(997, 40, 11_000, 25), 11_100).kind).not.toBe('arrived');
  });

  it('does not let an outside-corridor observation move the accepted search anchor', () => {
    const tracker = new NavigationProgressTracker(route({
      geometry: Array.from({ length: 11 }, (_, index) => point(index * 100)),
      policy: { ...policy, searchBackM: 100, searchForwardM: 300 },
    }));
    expect(tracker.accept(fix(100), 10_100).kind).toBe('guidance');
    expect(tracker.accept(fix(390, 100, 11_000), 11_100).kind).toBe('uncertain');
    expect(tracker.accept(fix(120, 0, 12_000), 12_100).kind).toBe('guidance');
  });

  it('bounds the first match near route start instead of jumping onto a later loop', () => {
    const tracker = new NavigationProgressTracker(route({
      geometry: [
        point(0, 0), point(0, 100), point(500, 100),
        point(500, 0), point(100, 0),
      ],
      policy: { ...policy, searchForwardM: 300 },
    }));
    const state = tracker.accept(fix(440, 0), 10_100);
    expect(state.kind).toBe('uncertain');
  });
});
