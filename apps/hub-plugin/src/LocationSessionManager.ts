import {
  AppLocationAccuracy,
  type AppLocation,
  type AppLocationOptions,
  type EvenAppBridge,
} from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { G2DiagnosticTrace, G2TraceCadence } from './G2DiagnosticTrace';
import type { MacLink } from './MacLink';

type LocationProfile = 'ambient' | 'probe' | 'navigation';
type NavigationLocationConsumer = (location: AppLocation, receivedAt: number) => void;

const AMBIENT_OPTIONS: Readonly<AppLocationOptions> = {
  accuracy: AppLocationAccuracy.Medium,
  distanceFilter: 250,
  intervalMs: 60_000,
};

/** Measurement request, not a final navigation threshold. */
const HIGH_ACCURACY_OPTIONS: Readonly<AppLocationOptions> = {
  accuracy: AppLocationAccuracy.High,
  distanceFilter: 1,
  intervalMs: 1000,
};

// The Even host owns the actual iPhone acquisition deadline. Keep a small
// local settling window after it: racing JavaScript at the exact same
// millisecond can discard a valid result crossing the Flutter/WebView bridge.
// The watchdog is only for hosts that never settle their promise.
const ONE_SHOT_LOCATION_TIMEOUT_MS = 5_000;
const ONE_SHOT_SETTLE_GRACE_MS = 1_000;
const ONE_SHOT_WATCHDOG_TIMEOUT_MS = ONE_SHOT_LOCATION_TIMEOUT_MS + ONE_SHOT_SETTLE_GRACE_MS;

// The continuous start call has the same host-boundary risk. A host that
// neither resolves nor rejects must become an explicit failed capability in
// the owner-only receipt, rather than holding the location state machine.
const STREAM_START_TIMEOUT_MS = 5_000;

const FIELD_TIMESTAMP = 1;
const FIELD_SPEED = 2;
const FIELD_HEADING = 4;
const EARTH_RADIUS_M = 6_371_000;

type LocationBridge = Pick<
  EvenAppBridge,
  'getAppLocation' | 'startAppLocationUpdates' | 'stopAppLocationUpdates' | 'onAppLocationChanged'
>;

/**
 * Sole owner of the Even host's unkeyed location-update stream.
 *
 * The host exposes one start/stop pair, so ambient grounding and the packaged
 * navigation probe cannot pretend to be independent subscriptions. This class
 * switches that one stream between profiles while keeping the ambient Mac
 * uplink's coarse five-minute/~500m contract unchanged.
 */
export class LocationSessionManager {
  private unsubscribe?: () => void;
  private desiredProfile: LocationProfile = 'ambient';
  private appliedProfile?: LocationProfile;
  private streamMayBeActive = false;
  private shouldRun = false;
  private operation: Promise<boolean> = Promise.resolve(true);
  private profileEpoch = 0;
  private foreground = true;
  private probeSession = false;
  private navigationConsumer?: NavigationLocationConsumer;

  private lastSent?: { lat: number; lon: number; at: number };
  private lastQuality?: { lat: number; lon: number; receivedAt: number };
  private qualitySequence = 0;

  constructor(
    private readonly client: Pick<BridgeClient, 'require'>,
    private readonly link: Pick<MacLink, 'send'>,
    private readonly diagnostics: G2DiagnosticTrace,
  ) {}

  start(): Promise<boolean> {
    this.shouldRun = true;
    return this.enqueue(() => this.startNow());
  }

  /** Explicit Gate-0 control. This is not inferred from speed or motion. */
  setProbeEnabled(enabled: boolean, signal?: AbortSignal): Promise<boolean> {
    if (enabled && this.navigationConsumer) return Promise.resolve(false);
    if (!enabled && this.navigationConsumer) return Promise.resolve(true);
    const profileEpoch = ++this.profileEpoch;
    if (enabled) this.probeSession = true;
    const desiredProfile = enabled ? 'probe' : 'ambient';
    if (this.desiredProfile !== desiredProfile) {
      this.desiredProfile = desiredProfile;
      this.resetQualityWindow();
    }
    const operation = this.enqueue(async () => {
      if (!this.shouldRun || this.unsubscribe === undefined) return true;
      return this.applyDesiredProfile(profileEpoch);
    });
    if (!enabled || signal === undefined) return operation;
    const revoke = (): void => { void this.revokeRemoteControl(); };
    if (signal.aborted) revoke();
    else signal.addEventListener('abort', revoke, { once: true });
    return operation.finally(() => signal.removeEventListener('abort', revoke));
  }

  /** Explicit navigation session. High-rate fixes stay on the phone. */
  setNavigationConsumer(consumer?: NavigationLocationConsumer): Promise<boolean> {
    if (consumer && this.probeSession) return Promise.resolve(false);
    const profileEpoch = ++this.profileEpoch;
    this.navigationConsumer = consumer;
    const desiredProfile = consumer ? 'navigation' : 'ambient';
    if (this.desiredProfile !== desiredProfile) {
      this.desiredProfile = desiredProfile;
      this.resetQualityWindow();
    }
    return this.enqueue(async () => {
      if (!this.shouldRun || this.unsubscribe === undefined) return true;
      return this.applyDesiredProfile(profileEpoch);
    });
  }

  /** Link loss revokes every Mac-authorized high-rate profile immediately. */
  revokeRemoteControl(): Promise<boolean> {
    const profileEpoch = ++this.profileEpoch;
    this.probeSession = false;
    this.navigationConsumer = undefined;
    this.desiredProfile = 'ambient';
    this.appliedProfile = undefined;
    this.resetQualityWindow();
    // Do not queue the first stop behind a hung native start. If that start
    // later reports success, its epoch check below issues a second stop.
    const immediateStop = this.forceStopStream();
    return this.enqueue(async () => {
      await immediateStop;
      if (!this.shouldRun || this.unsubscribe === undefined) return true;
      return this.applyDesiredProfile(profileEpoch);
    });
  }

  /**
   * The host may suspend a live WebView's location stream while backgrounded.
   * On return, retain the user-authorized profile but force its one host
   * subscription to start again. If the WebView was actually killed, normal
   * bootstrap/reconnect owns recovery instead.
   */
  setForeground(foreground: boolean): Promise<boolean> {
    const resumed = foreground && !this.foreground;
    this.foreground = foreground;
    if (!resumed) return Promise.resolve(true);

    const profileEpoch = ++this.profileEpoch;
    // `applyDesiredProfile` normally avoids duplicate starts for the same
    // profile. A foreground transition is the deliberate exception: the host
    // can have stopped it without notifying JavaScript.
    this.appliedProfile = undefined;
    return this.enqueue(async () => {
      if (!this.shouldRun || this.unsubscribe === undefined || !this.foreground) return true;
      return this.applyDesiredProfile(profileEpoch);
    });
  }

  /** Fresh, explicit route origin; does not change the continuous profile. */
  async requestNavigationOrigin(): Promise<AppLocation | null> {
    try {
      // Use the same host-boundary timeout as ambient and assistant reads.
      // Some Even host builds acknowledge the request but never settle it;
      // route setup must recover to an actionable error instead of remaining
      // indefinitely on the locating HUD.
      return await this.readOneShotLocation(this.bridge(), AppLocationAccuracy.High);
    } catch {
      return null;
    }
  }

  /**
   * Answer a Mac-requested assistant lookup with one current phone fix.
   * This deliberately stays medium accuracy and does not change the ambient
   * stream: it is local context, not an implicit navigation session.
   */
  async requestAssistantLocation(requestId: string, signal?: AbortSignal): Promise<boolean> {
    if (!isRequestId(requestId) || signal?.aborted) return false;
    try {
      const location = await this.readOneShotLocation(this.bridge(), AppLocationAccuracy.Medium);
      if (signal?.aborted || location === null) return false;
      return this.forwardAmbient(location, Date.now(), true, requestId);
    } catch {
      return false;
    }
  }

  shutdown(): Promise<boolean> {
    this.profileEpoch += 1;
    this.shouldRun = false;
    this.desiredProfile = 'ambient';
    return this.enqueue(async () => {
      const stopped = await this.stopStreamIfNeeded();
      this.appliedProfile = undefined;
      this.unsubscribe?.();
      this.unsubscribe = undefined;
      this.resetQualityWindow();
      this.probeSession = false;
      this.navigationConsumer = undefined;
      return stopped;
    });
  }

  private enqueue(action: () => Promise<boolean>): Promise<boolean> {
    this.operation = this.operation.then(action, action);
    return this.operation;
  }

  private async startNow(): Promise<boolean> {
    const profileEpoch = this.profileEpoch;
    if (!this.shouldRun) return false;
    if (this.unsubscribe !== undefined) return this.applyDesiredProfile(profileEpoch);

    let bridge: LocationBridge;
    try {
      bridge = this.bridge();
      // Register first: a fast host push must not land between start and listen.
      this.unsubscribe = bridge.onAppLocationChanged((location) => this.onLocation(location));
    } catch {
      this.recordProfileState(this.desiredProfile, false);
      return false;
    }

    try {
      const first = await this.readInitialLocation(bridge);
      if (first) this.onLocation(first);
    } catch {
      // A failed one-shot does not prove continuous updates are unavailable.
    }

    const started = await this.applyDesiredProfile(profileEpoch);
    if (!started && this.appliedProfile === undefined) {
      this.unsubscribe?.();
      this.unsubscribe = undefined;
    }
    return started;
  }

  private async applyDesiredProfile(profileEpoch = this.profileEpoch): Promise<boolean> {
    if (!this.shouldRun) return true;
    const target = this.desiredProfile;
    if (this.appliedProfile === target) return true;

    const bridge = this.bridge();
    if (this.streamMayBeActive) {
      // A restart is still worth attempting if stop fails; the host may
      // already be stopped. Retain uncertainty for post-start cleanup.
      await this.stopStreamIfNeeded();
      this.appliedProfile = undefined;
    }
    if (profileEpoch !== this.profileEpoch || target !== this.desiredProfile) return false;

    try {
      // Even a rejected/throwing bridge call can leave the native side in an
      // unknown state. Retain that uncertainty so shutdown retries stop.
      this.streamMayBeActive = true;
      const started = await this.startLocationUpdates(
        bridge,
        target === 'ambient' ? AMBIENT_OPTIONS : HIGH_ACCURACY_OPTIONS,
      );
      if (
        profileEpoch !== this.profileEpoch
        || target !== this.desiredProfile
        || !this.shouldRun
      ) {
        await this.forceStopStream();
        return false;
      }
      if (!started) {
        await this.stopStreamIfNeeded();
        this.recordProfileState(target, false);
        return false;
      }
      this.appliedProfile = target;
      this.recordProfileState(target, true);
      return true;
    } catch {
      if (profileEpoch !== this.profileEpoch || target !== this.desiredProfile) {
        await this.forceStopStream();
        return false;
      }
      await this.stopStreamIfNeeded();
      this.recordProfileState(target, false);
      return false;
    }
  }

  private async stopStreamIfNeeded(): Promise<boolean> {
    if (!this.streamMayBeActive) return true;
    try {
      const stopped = await this.bridge().stopAppLocationUpdates();
      if (stopped) this.streamMayBeActive = false;
      return stopped;
    } catch {
      return false;
    }
  }

  private async forceStopStream(): Promise<boolean> {
    try {
      const stopped = await this.bridge().stopAppLocationUpdates();
      if (stopped) this.streamMayBeActive = false;
      return stopped;
    } catch {
      return false;
    }
  }

  private bridge(): LocationBridge {
    return this.client.require();
  }

  private async readInitialLocation(bridge: LocationBridge): Promise<AppLocation | null> {
    return this.readOneShotLocation(
      bridge,
      this.desiredProfile === 'ambient' ? AppLocationAccuracy.Medium : AppLocationAccuracy.High,
    );
  }

  private async readOneShotLocation(
    bridge: LocationBridge,
    accuracy: AppLocationAccuracy,
  ): Promise<AppLocation | null> {
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        bridge.getAppLocation({
          accuracy,
          timeoutMs: ONE_SHOT_LOCATION_TIMEOUT_MS,
        }),
        new Promise<null>((resolve) => {
          timeout = setTimeout(() => resolve(null), ONE_SHOT_WATCHDOG_TIMEOUT_MS);
        }),
      ]);
    } finally {
      if (timeout !== undefined) clearTimeout(timeout);
    }
  }

  private async startLocationUpdates(
    bridge: LocationBridge,
    options: Readonly<AppLocationOptions>,
  ): Promise<boolean> {
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const start = Promise.resolve().then(() => bridge.startAppLocationUpdates(options));
    try {
      const result = await Promise.race([
        start,
        new Promise<null>((resolve) => {
          timeout = setTimeout(() => resolve(null), STREAM_START_TIMEOUT_MS);
        }),
      ]);
      if (result !== null) return result;

      // The native call may still take effect after its promise became stale.
      // Stop now, and compensate once more if the late promise eventually
      // resolves. Neither cleanup task may keep the diagnostic control path
      // waiting.
      void this.forceStopStream();
      void start.then(
        () => this.forceStopStream(),
        () => undefined,
      );
      return false;
    } finally {
      if (timeout !== undefined) clearTimeout(timeout);
    }
  }

  private recordProfileState(profile: LocationProfile, success: boolean): void {
    if (profile === 'navigation') return;
    if (!this.probeSession) return;
    this.diagnostics.record({
      type: 'location_probe_state',
      requested: profile === 'probe' ? 'high' : 'ambient',
      outcome: success ? 'success' : 'failed',
    });
    if (profile === 'ambient' && success) this.probeSession = false;
  }

  private onLocation(location: AppLocation): void {
    const latitude = location.latitude;
    const longitude = location.longitude;
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return;

    const receivedAt = Date.now();
    this.forwardAmbient(location, receivedAt);
    if (this.appliedProfile === 'probe') {
      this.recordLocationQuality(location, receivedAt, 'location_probe');
    }
    if (this.appliedProfile === 'navigation') {
      this.recordLocationQuality(location, receivedAt, 'navigation_location');
      this.navigationConsumer?.(location, receivedAt);
    }
  }

  private forwardAmbient(
    location: AppLocation,
    receivedAt: number,
    force = false,
    refreshId?: string,
  ): boolean {
    const latitude = location.latitude;
    const longitude = location.longitude;
    const accuracy = location.accuracy;
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return false;
    if (!force && this.lastSent) {
      const dLat = Math.abs(latitude - this.lastSent.lat);
      const dLon = Math.abs(longitude - this.lastSent.lon);
      const moved = dLat > 0.005 || dLon > 0.005; // ~500m
      const stale = receivedAt - this.lastSent.at > 300_000;
      if (!moved && !stale) return false;
    }
    this.lastSent = { lat: latitude, lon: longitude, at: receivedAt };
    const sourceTimestamp = location.timestamp;
    const timestamp = Number.isSafeInteger(sourceTimestamp) && sourceTimestamp !== undefined && sourceTimestamp >= 0
      ? sourceTimestamp
      : undefined;
    this.link.send({
      kind: 'location',
      latitude,
      longitude,
      ...(Number.isFinite(accuracy) ? { accuracy } : {}),
      ...(timestamp !== undefined ? { timestamp } : {}),
      ...(refreshId !== undefined ? { refreshId } : {}),
    });
    return true;
  }

  private recordLocationQuality(
    location: AppLocation,
    receivedAt: number,
    kind: 'location_probe' | 'navigation_location',
  ): void {
    const previous = this.lastQuality;
    const cadence = cadenceBucket(previous ? receivedAt - previous.receivedAt : undefined);
    const movement = previous
      ? displacementAndBearing(previous.lat, previous.lon, location.latitude, location.longitude)
      : undefined;

    this.qualitySequence = this.qualitySequence >= 2_147_483_647
      ? 1
      : this.qualitySequence + 1;
    const lifecycle: 'foreground' | 'background' = this.foreground
      ? 'foreground'
      : 'background';
    const quality = {
      sampleSequence: this.qualitySequence,
      lifecycle,
      fields: fieldMask(location),
      cadence,
      accuracy: accuracyBucket(location.accuracy),
      timestampAge: timestampAgeBucket(location.timestamp, receivedAt),
      displacement: displacementBucket(movement?.distance),
      headingDelta: headingDeltaBucket(location.heading, movement),
    };
    if (kind === 'location_probe') {
      this.diagnostics.record({ type: kind, profile: 'high', ...quality });
    } else {
      this.diagnostics.record({ type: kind, ...quality });
    }
    this.lastQuality = {
      lat: location.latitude,
      lon: location.longitude,
      receivedAt,
    };
  }

  private resetQualityWindow(): void {
    this.lastQuality = undefined;
    this.qualitySequence = 0;
  }
}

function isRequestId(value: string): boolean {
  return value.length > 0 && value.length <= 64;
}

function fieldMask(location: AppLocation): number {
  return (Number.isFinite(location.timestamp) ? FIELD_TIMESTAMP : 0)
    | (Number.isFinite(location.speed) ? FIELD_SPEED : 0)
    | (Number.isFinite(location.heading) ? FIELD_HEADING : 0);
}

function accuracyBucket(accuracy: number | undefined): 'unknown' | 'lte5' | 'lte10' | 'lte25' | 'gt25' {
  if (!Number.isFinite(accuracy) || (accuracy as number) < 0) return 'unknown';
  if ((accuracy as number) <= 5) return 'lte5';
  if ((accuracy as number) <= 10) return 'lte10';
  if ((accuracy as number) <= 25) return 'lte25';
  return 'gt25';
}

function timestampAgeBucket(
  timestamp: number | undefined,
  receivedAt: number,
): 'missing' | 'future' | 'fresh' | 'stale' {
  if (!Number.isFinite(timestamp)) return 'missing';
  const age = receivedAt - (timestamp as number);
  if (age < -1000) return 'future';
  return age <= 5000 ? 'fresh' : 'stale';
}

function displacementBucket(
  distance: number | undefined,
): 'first' | 'lt5' | '5_20' | '20_100' | 'gt100' {
  if (distance === undefined) return 'first';
  if (distance < 5) return 'lt5';
  if (distance < 20) return '5_20';
  if (distance < 100) return '20_100';
  return 'gt100';
}

function headingDeltaBucket(
  heading: number | undefined,
  movement: { distance: number; bearing: number } | undefined,
): 'unavailable' | 'insufficient_motion' | '0_30' | '31_90' | '91_180' {
  if (!Number.isFinite(heading) || movement === undefined) return 'unavailable';
  if (movement.distance < 5) return 'insufficient_motion';
  const delta = Math.abs((((heading as number) - movement.bearing + 540) % 360) - 180);
  if (delta <= 30) return '0_30';
  if (delta <= 90) return '31_90';
  return '91_180';
}

function displacementAndBearing(
  fromLatitude: number,
  fromLongitude: number,
  toLatitude: number,
  toLongitude: number,
): { distance: number; bearing: number } {
  const fromLat = degreesToRadians(fromLatitude);
  const toLat = degreesToRadians(toLatitude);
  const deltaLat = toLat - fromLat;
  const deltaLon = degreesToRadians(toLongitude - fromLongitude);
  const sinHalfLat = Math.sin(deltaLat / 2);
  const sinHalfLon = Math.sin(deltaLon / 2);
  const a = sinHalfLat * sinHalfLat
    + Math.cos(fromLat) * Math.cos(toLat) * sinHalfLon * sinHalfLon;
  const distance = EARTH_RADIUS_M * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  const y = Math.sin(deltaLon) * Math.cos(toLat);
  const x = Math.cos(fromLat) * Math.sin(toLat)
    - Math.sin(fromLat) * Math.cos(toLat) * Math.cos(deltaLon);
  const bearing = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  return { distance, bearing };
}

function degreesToRadians(degrees: number): number {
  return degrees * Math.PI / 180;
}

function cadenceBucket(intervalMs: number | undefined): G2TraceCadence {
  if (intervalMs === undefined || !Number.isFinite(intervalMs)) return 'first';
  if (intervalMs <= 1_000) return 'lte1s';
  if (intervalMs <= 5_000) return '1_5s';
  if (intervalMs <= 30_000) return '5_30s';
  return 'gt30s';
}
