import type { NavigationMapModel, NavigationMapPoint, NavigationMode } from './NavigationHud';

/**
 * The guidance pane's ROUTE SCHEMATIC: pure geometry, no canvas.
 *
 * Until 2026-11-23 the right pane composited a phone-rendered 4-bit street
 * tile under projected route rails. The wearer's verdict: noisy, unlike the
 * phone's map, and "I can't tell where I am". Two independently drawn layers
 * (a tile with its own centre/zoom/cache, and a route projected over it)
 * could disagree, buildings quantized to noise, and the picture was
 * north-up while the wearer faced some other way.
 *
 * The schematic is drawn from exactly two inputs — the route polyline and
 * the live fix — through ONE local projection, so the "you" marker and the
 * line cannot drift apart. It is HEADING-UP: the wearer's direction of
 * travel always points to the top of the pane, and "you" sits at a fixed
 * anchor in the lower centre, so the question "where am I" is answered by
 * where the eye already looks.
 */

export type ScreenPoint = readonly [number, number];

export interface SchematicBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface RouteSchematic {
  /** The fixed "you" anchor; the chevron always points up from here. */
  you: ScreenPoint;
  /** Degrees clockwise from true north that the pane's "up" faces. */
  headingDeg: number;
  /** Whether `headingDeg` came from the fix (true) or the route (false). */
  headingFromFix: boolean;
  metresPerPixel: number;
  /** Route already behind the wearer, start → snapped position. */
  travelled: ScreenPoint[];
  /** Route still ahead, snapped position → destination. Drawn bright. */
  remaining: ScreenPoint[];
  /** The next maneuver, when the model says where it is. */
  maneuver?: {
    point: ScreenPoint;
    label?: string;
    /** Which side of the point the route leaves towards on screen. */
    exitSide: 'left' | 'right' | 'ahead';
    /** Faint cross-street stubs at the intersection. */
    stubs: Array<readonly [ScreenPoint, ScreenPoint]>;
  };
  /** The route's end, when it falls inside the pane. */
  destination?: ScreenPoint;
  /** True when the fix is too far from the line to snap onto it. */
  offRoute: boolean;
  /** Off route: where the line is nearest, for a faint connector. */
  nearestOnRoute?: ScreenPoint;
}

/** Where "you" sits: horizontally centred, four fifths of the way down. */
export const SCHEMATIC_ANCHOR_X = 0.5;
export const SCHEMATIC_ANCHOR_Y = 0.8;
/** Space kept above the farthest fitted point for the street label. */
const TOP_MARGIN_PX = 28;
const SIDE_MARGIN_PX = 16;
/** A fix within this distance of the line is drawn ON the line. */
export const SCHEMATIC_SNAP_M = 30;
/** Below this ground speed the phone's course is noise, not direction. */
const MIN_COURSE_SPEED_MPS = 0.5;
const ROUTE_BEARING_LOOKAHEAD_M = 12;
const STUB_PX = 22;
const EARTH_RADIUS_M = 6_371_000;

/** Forward ground span the pane shows, by mode: never so close the next
 * block is out of view, never so far a street becomes a hairline. */
export const SCHEMATIC_SPAN_M: Readonly<Record<NavigationMode, { min: number; max: number }>> = {
  walking: { min: 60, max: 500 },
  cycling: { min: 100, max: 900 },
  driving: { min: 150, max: 2_000 },
};

interface XY { x: number; y: number }

interface Projected {
  points: XY[];
  /** Cumulative metres along the route at each vertex. */
  along: number[];
}

function toLocal(origin: NavigationMapPoint, point: NavigationMapPoint): XY {
  const lat0 = origin.latitude * Math.PI / 180;
  return {
    x: (point.longitude - origin.longitude) * Math.PI / 180 * EARTH_RADIUS_M * Math.cos(lat0),
    y: (point.latitude - origin.latitude) * Math.PI / 180 * EARTH_RADIUS_M,
  };
}

function project(geometry: readonly NavigationMapPoint[], origin: NavigationMapPoint): Projected {
  const points = geometry.map((point) => toLocal(origin, point));
  const along = [0];
  for (let index = 1; index < points.length; index += 1) {
    const a = points[index - 1]!;
    const b = points[index]!;
    along.push(along[index - 1]! + Math.hypot(b.x - a.x, b.y - a.y));
  }
  return { points, along };
}

/** The point `metres` along the route, clamped to its ends. */
function pointAlong(route: Projected, metres: number): XY {
  const { points, along } = route;
  if (metres <= 0) return points[0]!;
  for (let index = 1; index < points.length; index += 1) {
    if (along[index]! >= metres) {
      const a = points[index - 1]!;
      const b = points[index]!;
      const length = along[index]! - along[index - 1]!;
      const t = length > 0 ? (metres - along[index - 1]!) / length : 0;
      return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
    }
  }
  return points[points.length - 1]!;
}

interface Snap { point: XY; alongM: number; crossM: number; segment: number }

/** Nearest point on the route to `target`. A tracker progress hint keeps a
 * route that doubles back on itself from snapping to the wrong pass. */
function snapToRoute(route: Projected, target: XY, progressHintM?: number): Snap {
  let best: Snap | undefined;
  const window = progressHintM !== undefined && Number.isFinite(progressHintM) ? 80 : Infinity;
  for (let pass = 0; pass < 2 && !best; pass += 1) {
    for (let index = 0; index < route.points.length - 1; index += 1) {
      const a = route.points[index]!;
      const b = route.points[index + 1]!;
      const startM = route.along[index]!;
      const endM = route.along[index + 1]!;
      if (pass === 0 && window !== Infinity
        && (endM < progressHintM! - window || startM > progressHintM! + window)) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const lengthSq = dx * dx + dy * dy;
      const t = lengthSq > 0
        ? Math.max(0, Math.min(1, ((target.x - a.x) * dx + (target.y - a.y) * dy) / lengthSq))
        : 0;
      const point = { x: a.x + dx * t, y: a.y + dy * t };
      const crossM = Math.hypot(target.x - point.x, target.y - point.y);
      if (!best || crossM < best.crossM) {
        best = { point, alongM: startM + (endM - startM) * t, crossM, segment: index };
      }
    }
  }
  return best!;
}

/** Bearing in degrees clockwise from north of the vector `from` → `to`. */
function bearing(from: XY, to: XY): number {
  const degrees = Math.atan2(to.x - from.x, to.y - from.y) * 180 / Math.PI;
  return (degrees + 360) % 360;
}

/** The phone's course when it is a real direction of travel. iOS reports
 * -1 for "no course"; a stationary wearer's course is whatever it last was. */
function fixHeading(map: NavigationMapModel): number | undefined {
  const heading = map.heading;
  if (heading === undefined || !Number.isFinite(heading) || heading < 0) return undefined;
  if (map.speed !== undefined && Number.isFinite(map.speed) && map.speed < MIN_COURSE_SPEED_MPS) {
    return undefined;
  }
  return heading % 360;
}

/** Snap a scale to quarter-octave steps so small moves do not breathe the
 * whole picture in and out between frames. */
function quantizeScale(metresPerPixel: number): number {
  return Math.pow(2, Math.round(Math.log2(metresPerPixel) * 4) / 4);
}

/**
 * Build the schematic for one guidance frame, or `undefined` when the route
 * has no usable shape. Before the first fix the wearer is placed at the
 * route's start, facing along it.
 */
export function buildRouteSchematic(
  map: NavigationMapModel,
  bounds: SchematicBounds,
  mode: NavigationMode = 'walking',
): RouteSchematic | undefined {
  if (map.geometry.length < 2) return undefined;
  const origin = map.position ?? map.geometry[0]!;
  const route = project(map.geometry, origin);
  const total = route.along[route.along.length - 1]!;
  if (!(total > 0.5)) return undefined;

  const fix: XY = { x: 0, y: 0 };
  const snap = snapToRoute(route, fix, map.progressM);
  const offRoute = snap.crossM > SCHEMATIC_SNAP_M;
  // On the line, "you" IS the line: GPS wobble of a few metres must not
  // slide the route sideways under a fixed marker. Off it, the gap is the
  // message, so the marker stays at the raw fix.
  const anchorWorld = offRoute ? fix : snap.point;
  const alongM = snap.alongM;

  const fromFix = fixHeading(map);
  let headingDeg: number;
  if (fromFix !== undefined) {
    headingDeg = fromFix;
  } else {
    const aheadM = Math.min(total, alongM + ROUTE_BEARING_LOOKAHEAD_M);
    const from = aheadM - alongM < 1 ? pointAlong(route, Math.max(0, total - ROUTE_BEARING_LOOKAHEAD_M)) : snap.point;
    const to = aheadM - alongM < 1 ? route.points[route.points.length - 1]! : pointAlong(route, aheadM);
    headingDeg = bearing(from, to);
  }
  const h = headingDeg * Math.PI / 180;
  const cos = Math.cos(h);
  const sin = Math.sin(h);
  /** Ground metres → (side, forward) relative to the anchor. */
  const rotate = (point: XY): XY => {
    const e = point.x - anchorWorld.x;
    const n = point.y - anchorWorld.y;
    return { x: e * cos - n * sin, y: e * sin + n * cos };
  };

  const anchorX = bounds.x + bounds.width * SCHEMATIC_ANCHOR_X;
  const anchorY = bounds.y + bounds.height * SCHEMATIC_ANCHOR_Y;
  const forwardPx = anchorY - bounds.y - TOP_MARGIN_PX;
  const backPx = bounds.y + bounds.height - anchorY - 6;
  const sidePx = bounds.width / 2 - SIDE_MARGIN_PX;

  // Zoom: the next maneuver plus a little of the road beyond it, inside
  // the mode's span limits.
  const span = SCHEMATIC_SPAN_M[mode] ?? SCHEMATIC_SPAN_M.walking;
  let metresPerPixel = span.min / forwardPx;
  const maneuverAlongM = map.maneuverAlongM !== undefined && Number.isFinite(map.maneuverAlongM)
    ? Math.max(0, Math.min(total, map.maneuverAlongM))
    : undefined;
  if (maneuverAlongM !== undefined) {
    const toManeuverM = Math.max(0, maneuverAlongM - alongM);
    const beyondM = Math.min(total, maneuverAlongM + Math.max(15, Math.min(120, toManeuverM * 0.3)));
    for (const metres of [maneuverAlongM, beyondM]) {
      const local = rotate(pointAlong(route, metres));
      if (local.y > 0) metresPerPixel = Math.max(metresPerPixel, local.y / forwardPx);
      else metresPerPixel = Math.max(metresPerPixel, -local.y / backPx);
      metresPerPixel = Math.max(metresPerPixel, Math.abs(local.x) / sidePx);
    }
  }
  metresPerPixel = quantizeScale(Math.max(span.min / forwardPx, Math.min(span.max / forwardPx, metresPerPixel)));

  const toScreen = (point: XY): ScreenPoint => {
    const local = rotate(point);
    return [anchorX + local.x / metresPerPixel, anchorY - local.y / metresPerPixel];
  };

  const splitIndex = snap.segment + 1;
  const travelled = [...route.points.slice(0, splitIndex), snap.point].map(toScreen);
  const remaining = [snap.point, ...route.points.slice(splitIndex)].map(toScreen);

  let maneuver: RouteSchematic['maneuver'];
  if (maneuverAlongM !== undefined && maneuverAlongM >= alongM - 1) {
    const centre = pointAlong(route, maneuverAlongM);
    const point = toScreen(centre);
    const before = toScreen(pointAlong(route, maneuverAlongM - 10));
    const after = toScreen(pointAlong(route, maneuverAlongM + 10));
    const unit = (from: ScreenPoint, to: ScreenPoint): ScreenPoint | undefined => {
      const length = Math.hypot(to[0] - from[0], to[1] - from[1]);
      return length > 0.5 ? [(to[0] - from[0]) / length, (to[1] - from[1]) / length] : undefined;
    };
    const incoming = unit(before, point);
    const outgoing = unit(point, after);
    const stubs: Array<readonly [ScreenPoint, ScreenPoint]> = [];
    let exitSide: 'left' | 'right' | 'ahead' = 'ahead';
    if (incoming && outgoing && maneuverAlongM < total - 1) {
      // The street the wearer is on carries on past the corner, and the
      // street they turn onto runs both ways: one stub each, so the corner
      // reads as an intersection rather than a bend in a lone line.
      const turn = incoming[0] * outgoing[1] - incoming[1] * outgoing[0];
      const straightness = incoming[0] * outgoing[0] + incoming[1] * outgoing[1];
      if (straightness < 0.97) {
        exitSide = turn > 0 ? 'right' : 'left';
        stubs.push([point, [point[0] + incoming[0] * STUB_PX, point[1] + incoming[1] * STUB_PX]]);
        stubs.push([point, [point[0] - outgoing[0] * STUB_PX, point[1] - outgoing[1] * STUB_PX]]);
      }
    }
    const label = map.maneuverLabel?.trim();
    maneuver = { point, exitSide, stubs, ...(label ? { label } : {}) };
  }

  const end = toScreen(route.points[route.points.length - 1]!);
  const inside = end[0] >= bounds.x && end[0] <= bounds.x + bounds.width
    && end[1] >= bounds.y && end[1] <= bounds.y + bounds.height;

  return {
    you: [anchorX, anchorY],
    headingDeg,
    headingFromFix: fromFix !== undefined,
    metresPerPixel,
    travelled,
    remaining,
    ...(maneuver ? { maneuver } : {}),
    ...(inside ? { destination: end } : {}),
    offRoute,
    ...(offRoute ? { nearestOnRoute: toScreen(snap.point) } : {}),
  };
}
