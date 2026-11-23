import { describe, expect, it } from 'vitest';

import { navigationStreetLabel, navigationVisualFixture, type NavigationMapModel } from './NavigationHud';
import {
  SCHEMATIC_ANCHOR_X,
  SCHEMATIC_ANCHOR_Y,
  SCHEMATIC_SPAN_M,
  buildRouteSchematic,
} from './NavigationSchematic';

const PANE = { x: 292, y: 28, width: 284, height: 220 };
const ANCHOR: readonly [number, number] = [
  PANE.x + PANE.width * SCHEMATIC_ANCHOR_X,
  PANE.y + PANE.height * SCHEMATIC_ANCHOR_Y,
];
const M = 1 / 111_195; // one metre of latitude (and of longitude at the equator), in degrees

/** North 200 m, then east 150 m (a right turn), at the equator. */
const RIGHT_TURN: NavigationMapModel = {
  geometry: [
    { latitude: 0, longitude: 0 },
    { latitude: 200 * M, longitude: 0 },
    { latitude: 200 * M, longitude: 150 * M },
  ],
  maneuverAlongM: 200,
  maneuverLabel: 'Michigan Ave',
};

const near = (a: readonly [number, number], b: readonly [number, number], tolerance = 1.5) =>
  Math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance;

describe('route schematic geometry', () => {
  it('anchors "you" at the fixed lower-centre point whatever the position', () => {
    for (const metres of [0, 60, 150]) {
      const s = buildRouteSchematic({ ...RIGHT_TURN, position: { latitude: metres * M, longitude: 0 } }, PANE)!;
      expect(s.you).toEqual(ANCHOR);
      // On the line, the route passes exactly through the anchor.
      expect(near(s.remaining[0]!, ANCHOR, 0.01)).toBe(true);
      expect(s.offRoute).toBe(false);
    }
  });

  it('is heading-up from the route when the fix has no course: ahead is straight up', () => {
    const s = buildRouteSchematic({ ...RIGHT_TURN, position: { latitude: 100 * M, longitude: 0 } }, PANE)!;
    expect(s.headingFromFix).toBe(false);
    expect(s.headingDeg).toBeCloseTo(0, 3);
    const corner = s.maneuver!.point;
    expect(corner[0]).toBeCloseTo(ANCHOR[0], 3);
    expect(corner[1]).toBeLessThan(ANCHOR[1]);
    // After the corner the line goes RIGHT on screen, and the label sits
    // on the other side of the node.
    const end = s.remaining[s.remaining.length - 1]!;
    expect(end[0]).toBeGreaterThan(corner[0]);
    expect(s.maneuver!.exitSide).toBe('right');
    expect(s.maneuver!.label).toBe('Michigan Ave');
  });

  it('rotates by the fix course: travelling east, the eastbound road is up and north is left', () => {
    // East 200 m, then north (a LEFT turn for an eastbound walker).
    const map: NavigationMapModel = {
      geometry: [
        { latitude: 0, longitude: 0 },
        { latitude: 0, longitude: 200 * M },
        { latitude: 150 * M, longitude: 200 * M },
      ],
      position: { latitude: 0, longitude: 80 * M },
      heading: 90,
      speed: 1.4,
      maneuverAlongM: 200,
    };
    const s = buildRouteSchematic(map, PANE)!;
    expect(s.headingFromFix).toBe(true);
    expect(s.headingDeg).toBe(90);
    const corner = s.maneuver!.point;
    expect(corner[0]).toBeCloseTo(ANCHOR[0], 3);
    expect(corner[1]).toBeLessThan(ANCHOR[1]);
    const end = s.remaining[s.remaining.length - 1]!;
    expect(end[0]).toBeLessThan(corner[0]);
    expect(s.maneuver!.exitSide).toBe('left');
  });

  it('follows the fix course over the route bearing, but not a stationary or invalid course', () => {
    const at = { ...RIGHT_TURN, position: { latitude: 100 * M, longitude: 0 } };
    expect(buildRouteSchematic({ ...at, heading: 20, speed: 2 }, PANE)!.headingDeg).toBe(20);
    expect(buildRouteSchematic({ ...at, heading: 20 }, PANE)!.headingDeg).toBe(20);
    expect(buildRouteSchematic({ ...at, heading: 20, speed: 0.1 }, PANE)!.headingDeg).toBeCloseTo(0, 3);
    expect(buildRouteSchematic({ ...at, heading: -1 }, PANE)!.headingDeg).toBeCloseTo(0, 3);
  });

  it('fits the next maneuver plus a little beyond, within the mode span limits', () => {
    const s = buildRouteSchematic({ ...RIGHT_TURN, position: { latitude: 50 * M, longitude: 0 } }, PANE)!;
    const corner = s.maneuver!.point;
    expect(corner[1]).toBeGreaterThan(PANE.y + 20);
    // Close to a corner the scale bottoms out at the mode minimum…
    const close = buildRouteSchematic({ ...RIGHT_TURN, position: { latitude: 195 * M, longitude: 0 } }, PANE)!;
    const forwardPx = ANCHOR[1] - PANE.y - 28;
    expect(close.metresPerPixel * forwardPx).toBeGreaterThanOrEqual(SCHEMATIC_SPAN_M.walking.min * 0.84);
    // …and a maneuver kilometres away never zooms past the maximum.
    const far: NavigationMapModel = {
      geometry: [{ latitude: 0, longitude: 0 }, { latitude: 5_000 * M, longitude: 0 }],
      position: { latitude: 0, longitude: 0 },
      maneuverAlongM: 5_000,
    };
    const farSchematic = buildRouteSchematic(far, PANE)!;
    expect(farSchematic.metresPerPixel * forwardPx).toBeLessThanOrEqual(SCHEMATIC_SPAN_M.walking.max * 1.2);
    const drive = buildRouteSchematic(far, PANE, 'driving')!;
    expect(drive.metresPerPixel).toBeGreaterThan(farSchematic.metresPerPixel);
  });

  it('splits the route at the wearer: travelled behind, remaining ahead', () => {
    const s = buildRouteSchematic({ ...RIGHT_TURN, position: { latitude: 100 * M, longitude: 3 * M } }, PANE)!;
    expect(near(s.travelled[s.travelled.length - 1]!, ANCHOR, 0.01)).toBe(true);
    expect(s.travelled[0]![1]).toBeGreaterThan(ANCHOR[1]);
    expect(s.remaining.every(([, y]) => y <= ANCHOR[1] + 0.01)).toBe(true);
  });

  it('marks the intersection with faint cross-street stubs at a turn', () => {
    const s = buildRouteSchematic({ ...RIGHT_TURN, position: { latitude: 100 * M, longitude: 0 } }, PANE)!;
    expect(s.maneuver!.stubs).toHaveLength(2);
    const [straightOn, crossStreet] = s.maneuver!.stubs;
    // Straight on continues up; the far side of the cross street goes left.
    expect(straightOn![1][1]).toBeLessThan(straightOn![0][1]);
    expect(crossStreet![1][0]).toBeLessThan(crossStreet![0][0]);
  });

  it('off route: "you" stays at the anchor, the line sits apart, and a connector points back', () => {
    const s = buildRouteSchematic({ ...RIGHT_TURN, position: { latitude: 100 * M, longitude: -80 * M } }, PANE)!;
    expect(s.offRoute).toBe(true);
    expect(s.you).toEqual(ANCHOR);
    expect(near(s.remaining[0]!, ANCHOR, 5)).toBe(false);
    // Heading-up from the route (north), the route is to the wearer's right.
    expect(s.nearestOnRoute![0]).toBeGreaterThan(ANCHOR[0]);
  });

  it('before the first fix, starts the wearer at the route start facing along it', () => {
    const s = buildRouteSchematic(RIGHT_TURN, PANE)!;
    expect(s.you).toEqual(ANCHOR);
    expect(s.headingDeg).toBeCloseTo(0, 3);
    expect(s.travelled.length).toBeLessThanOrEqual(2);
  });

  it('returns nothing for a route with no usable shape', () => {
    expect(buildRouteSchematic({ geometry: [{ latitude: 0, longitude: 0 }] }, PANE)).toBeUndefined();
    expect(buildRouteSchematic({
      geometry: [{ latitude: 0, longitude: 0 }, { latitude: 0, longitude: 0 }],
    }, PANE)).toBeUndefined();
  });

  it('draws the development fixture as a right turn ahead', () => {
    const fixture = navigationVisualFixture('nav-guidance');
    const s = buildRouteSchematic(fixture.map!, PANE)!;
    expect(s.maneuver?.exitSide).toBe('right');
    expect(s.maneuver?.label).toBe('W Fullerton Ave');
  });
});

describe('street label for the schematic', () => {
  it('takes the street an instruction names', () => {
    expect(navigationStreetLabel('Turn right onto N Michigan Ave')).toBe('N Michigan Ave');
    expect(navigationStreetLabel('Head east on E Randolph St')).toBe('E Randolph St');
    expect(navigationStreetLabel('Arrive at Millennium Park')).toBe('Millennium Park');
    expect(navigationStreetLabel('onto Test Ave')).toBe('Test Ave');
    expect(navigationStreetLabel('Continue')).toBe('Continue');
    expect(navigationStreetLabel('   ')).toBeUndefined();
    expect(navigationStreetLabel('Turn left onto The Very Long Name Of A Boulevard Extension')!.length)
      .toBeLessThanOrEqual(28);
  });
});
