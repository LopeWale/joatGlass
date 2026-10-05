import type { NavigationManeuver, NavigationMode } from './NavigationHud';

const EARTH_RADIUS_M = 6_371_000;

export interface NavigationCoordinate {
  latitude: number;
  longitude: number;
}

export interface NavigationFix extends NavigationCoordinate {
  accuracy?: number;
  heading?: number;
  speed?: number;
  timestamp?: number;
}

export interface NavigationRouteStep {
  id: string;
  maneuver: NavigationManeuver;
  instruction: string;
  distanceAlongRouteM: number;
}

/** Candidate per-mode policy. Release values require recorded replay and field evidence. */
export interface NavigationProgressPolicy {
  maxAccuracyM: number;
  staleAfterMs: number;
  corridorRadiusM: number;
  ambiguityMarginM: number;
  ambiguityProgressDeltaM: number;
  searchBackM: number;
  searchForwardM: number;
  minCourseDisplacementM: number;
  wrongWayAngleDeg: number;
  wrongWayMinBacktrackM: number;
  wrongWaySamples: number;
  offRouteSamples: number;
  arrivalRadiusM: number;
  arrivalSamples: number;
  uTurnSuppressionM: number;
}

export interface NavigationRoutePlan {
  mode: NavigationMode;
  geometry: readonly NavigationCoordinate[];
  steps: readonly NavigationRouteStep[];
  policy: NavigationProgressPolicy;
}

export type NavigationProgressState =
  | {
    kind: 'guidance';
    progressM: number;
    crossTrackM: number;
    maneuver: NavigationManeuver;
    instruction: string;
    maneuverDistanceM: number;
    remainingDistanceM: number;
  }
  | {
    kind: 'uncertain';
    reason: 'accuracy' | 'stale' | 'ambiguous' | 'off_route_pending';
  }
  | { kind: 'wrong_way'; progressM: number; remainingDistanceM: number }
  | { kind: 'off_route'; progressM: number; crossTrackM: number }
  | { kind: 'arrived'; progressM: number };

interface XY {
  x: number;
  y: number;
}

interface Segment {
  index: number;
  from: XY;
  to: XY;
  startM: number;
  lengthM: number;
  dx: number;
  dy: number;
}

interface Match {
  segment: Segment;
  alongM: number;
  crossTrackM: number;
}

interface AcceptedFix {
  point: XY;
  match: Match;
}

/**
 * Pure, local route progress engine. It consumes high-rate phone fixes and
 * emits semantic states without sending a coordinate round trip to the Mac.
 */
export class NavigationProgressTracker {
  private readonly origin: NavigationCoordinate;
  private readonly segments: readonly Segment[];
  private readonly totalDistanceM: number;
  private readonly destination: XY;
  private lastAccepted?: AcceptedFix;
  private furthestProgressM = 0;
  private wrongWayCount = 0;
  private offRouteCount = 0;
  private arrivalCount = 0;

  constructor(private readonly route: NavigationRoutePlan) {
    if (route.geometry.length < 2) throw new Error('navigation route requires geometry');
    if (route.steps.length < 1) throw new Error('navigation route requires steps');
    this.validatePolicy(route.policy);
    this.origin = route.geometry[0]!;
    const points = route.geometry.map((coordinate) => this.toXY(coordinate));
    this.destination = points[points.length - 1]!;
    const segments: Segment[] = [];
    let cumulative = 0;
    for (let index = 0; index < points.length - 1; index += 1) {
      const from = points[index]!;
      const to = points[index + 1]!;
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const lengthM = Math.hypot(dx, dy);
      if (lengthM < 0.01) continue;
      segments.push({ index, from, to, startM: cumulative, lengthM, dx, dy });
      cumulative += lengthM;
    }
    if (segments.length < 1) throw new Error('navigation route geometry is degenerate');
    this.segments = segments;
    this.totalDistanceM = cumulative;
  }

  accept(fix: NavigationFix, receivedAtMs: number): NavigationProgressState {
    const policy = this.route.policy;
    // A host that reports no accuracy figure is not reporting a BAD fix. The
    // Even SDK's `accuracy` is optional, and treating its absence as "worse
    // than the limit" left the guidance page on "Finding your position" for
    // the whole walk. Such a fix is trusted at the policy's limit: it gets
    // the widest corridor the policy allows, and nothing more.
    const accuracyM = Number.isFinite(fix.accuracy) ? (fix.accuracy as number) : policy.maxAccuracyM;
    if (accuracyM < 0 || accuracyM > policy.maxAccuracyM) {
      this.resetTransientEvidence();
      return { kind: 'uncertain', reason: 'accuracy' };
    }
    if (Number.isFinite(fix.timestamp)) {
      const age = receivedAtMs - (fix.timestamp as number);
      if (age < -1_000 || age > policy.staleAfterMs) {
        this.resetTransientEvidence();
        return { kind: 'uncertain', reason: 'stale' };
      }
    }

    const point = this.toXY(fix);
    const matches = this.matches(point);
    const best = matches[0];
    if (!best) {
      this.resetTransientEvidence();
      return { kind: 'uncertain', reason: 'ambiguous' };
    }
    const alternate = matches[1];
    if (
      alternate
      && alternate.crossTrackM - best.crossTrackM <= policy.ambiguityMarginM
      && Math.abs(alternate.alongM - best.alongM) >= policy.ambiguityProgressDeltaM
    ) {
      this.resetTransientEvidence();
      return { kind: 'uncertain', reason: 'ambiguous' };
    }

    const corridor = policy.corridorRadiusM + Math.min(accuracyM, policy.maxAccuracyM);
    if (best.crossTrackM > corridor) {
      this.offRouteCount += 1;
      this.wrongWayCount = 0;
      this.arrivalCount = 0;
      if (this.offRouteCount >= policy.offRouteSamples) {
        return {
          kind: 'off_route',
          progressM: Math.round(best.alongM),
          crossTrackM: Math.round(best.crossTrackM),
        };
      }
      return { kind: 'uncertain', reason: 'off_route_pending' };
    }
    this.offRouteCount = 0;

    const remainingM = Math.max(0, this.totalDistanceM - best.alongM);
    const endpointDistanceM = Math.hypot(
      point.x - this.destination.x,
      point.y - this.destination.y,
    );
    if (remainingM <= policy.arrivalRadiusM && endpointDistanceM <= policy.arrivalRadiusM) {
      this.arrivalCount += 1;
      this.lastAccepted = { point, match: best };
      this.furthestProgressM = Math.max(this.furthestProgressM, best.alongM);
      if (this.arrivalCount >= policy.arrivalSamples) {
        return { kind: 'arrived', progressM: Math.round(best.alongM) };
      }
    } else {
      this.arrivalCount = 0;
    }

    const movement = this.lastAccepted
      ? { x: point.x - this.lastAccepted.point.x, y: point.y - this.lastAccepted.point.y }
      : undefined;
    const displacementM = movement ? Math.hypot(movement.x, movement.y) : 0;
    const backtrackM = Math.max(0, this.furthestProgressM - best.alongM);
    const expectedUTurn = this.route.steps.some((step) => (
      step.maneuver === 'u_turn'
      && Math.abs(step.distanceAlongRouteM - best.alongM) <= policy.uTurnSuppressionM
    ));
    let opposing = false;
    if (movement && displacementM >= policy.minCourseDisplacementM) {
      const dot = movement.x * best.segment.dx + movement.y * best.segment.dy;
      const denominator = displacementM * best.segment.lengthM;
      const angle = Math.acos(Math.max(-1, Math.min(1, dot / denominator))) * 180 / Math.PI;
      opposing = angle >= policy.wrongWayAngleDeg;
    }
    if (opposing && backtrackM >= policy.wrongWayMinBacktrackM && !expectedUTurn) {
      this.wrongWayCount += 1;
    } else if (!opposing || expectedUTurn) {
      this.wrongWayCount = 0;
    }

    this.lastAccepted = { point, match: best };
    this.furthestProgressM = Math.max(this.furthestProgressM, best.alongM);
    if (this.wrongWayCount >= policy.wrongWaySamples) {
      return {
        kind: 'wrong_way',
        progressM: Math.round(best.alongM),
        remainingDistanceM: Math.round(remainingM),
      };
    }

    const step = this.route.steps.find(
      (candidate) => candidate.distanceAlongRouteM > best.alongM + 2,
    ) ?? this.route.steps[this.route.steps.length - 1]!;
    return {
      kind: 'guidance',
      progressM: Math.round(best.alongM),
      crossTrackM: Math.round(best.crossTrackM),
      maneuver: step.maneuver,
      instruction: step.instruction,
      maneuverDistanceM: Math.round(Math.max(0, step.distanceAlongRouteM - best.alongM)),
      remainingDistanceM: Math.round(remainingM),
    };
  }

  private matches(point: XY): Match[] {
    const anchor = this.lastAccepted?.match.alongM ?? 0;
    const minimumProgress = Math.max(0, anchor - this.route.policy.searchBackM);
    const maximumProgress = Math.min(
      this.totalDistanceM,
      anchor + this.route.policy.searchForwardM,
    );
    const candidates = this.segments.filter((segment) => (
      segment.startM + segment.lengthM >= minimumProgress
      && segment.startM <= maximumProgress
    ));
    return candidates.map((segment) => {
      const relativeX = point.x - segment.from.x;
      const relativeY = point.y - segment.from.y;
      const denominator = segment.lengthM * segment.lengthM;
      const fraction = Math.max(0, Math.min(
        1,
        (relativeX * segment.dx + relativeY * segment.dy) / denominator,
      ));
      const projectedX = segment.from.x + segment.dx * fraction;
      const projectedY = segment.from.y + segment.dy * fraction;
      return {
        segment,
        alongM: segment.startM + segment.lengthM * fraction,
        crossTrackM: Math.hypot(point.x - projectedX, point.y - projectedY),
      };
    }).filter((match) => (
      match.alongM >= minimumProgress && match.alongM <= maximumProgress
    )).sort((left, right) => left.crossTrackM - right.crossTrackM);
  }

  private toXY(coordinate: NavigationCoordinate): XY {
    const referenceLat = this.origin.latitude * Math.PI / 180;
    return {
      x: (coordinate.longitude - this.origin.longitude) * Math.PI / 180
        * EARTH_RADIUS_M * Math.cos(referenceLat),
      y: (coordinate.latitude - this.origin.latitude) * Math.PI / 180 * EARTH_RADIUS_M,
    };
  }

  private resetTransientEvidence(): void {
    this.wrongWayCount = 0;
    this.offRouteCount = 0;
    this.arrivalCount = 0;
  }

  private validatePolicy(policy: NavigationProgressPolicy): void {
    const positives = Object.entries(policy).filter(([name]) => name !== 'wrongWayAngleDeg');
    if (positives.some(([, value]) => !Number.isFinite(value) || value <= 0)) {
      throw new Error('navigation policy values must be positive and finite');
    }
    if (!Number.isFinite(policy.wrongWayAngleDeg)
      || policy.wrongWayAngleDeg <= 90 || policy.wrongWayAngleDeg > 180) {
      throw new Error('navigation wrong-way angle is invalid');
    }
  }
}
