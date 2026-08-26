import { AppLocationAccuracy, type AppLocation } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';

/**
 * Wearer position → Mac, via the sideload-safe API.
 *
 * `navigator.geolocation` does not work in a QR-sideloaded plugin;
 * `bridge.getAppLocation()` works in both (docs/HUB_PLUGIN.md). Without
 * this the brain answered place-and-timezone questions from the Mac's
 * point of view — confidently wrong the moment the wearer travels.
 *
 * Throttled hard: the Mac needs "roughly where", not a track log. A fix is
 * forwarded when it moved ~500m or is 5 minutes fresher than the last one.
 */
export class LocationMonitor {
  private stop?: () => void;
  private lastSent?: { lat: number; lon: number; at: number };

  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
  ) {}

  async start(): Promise<boolean> {
    const bridge = this.client.require();
    try {
      const first = await bridge.getAppLocation({
        accuracy: AppLocationAccuracy.Medium,
        timeoutMs: 5000,
      });
      if (first) this.forward(first);
      await bridge.startAppLocationUpdates({
        accuracy: AppLocationAccuracy.Medium,
        distanceFilter: 250,
        intervalMs: 60_000,
      });
      this.stop = bridge.onAppLocationChanged((loc) => this.forward(loc));
      return true;
    } catch {
      // No permission / older host: the Mac grounds "location unknown"
      // and the assistant asks for a city instead of inventing one.
      return false;
    }
  }

  async shutdown(): Promise<void> {
    this.stop?.();
    this.stop = undefined;
    try {
      await this.client.require().stopAppLocationUpdates();
    } catch {
      // Never started or host gone — nothing to stop.
    }
  }

  private forward(loc: AppLocation): void {
    const lat = loc.latitude;
    const lon = loc.longitude;
    if (typeof lat !== 'number' || typeof lon !== 'number') return;
    const now = Date.now();
    if (this.lastSent) {
      const dLat = Math.abs(lat - this.lastSent.lat);
      const dLon = Math.abs(lon - this.lastSent.lon);
      const moved = dLat > 0.005 || dLon > 0.005; // ~500m
      const stale = now - this.lastSent.at > 300_000;
      if (!moved && !stale) return;
    }
    this.lastSent = { lat, lon, at: now };
    this.link.send({
      kind: 'location',
      latitude: lat,
      longitude: lon,
      ...(typeof loc.accuracy === 'number' ? { accuracy: loc.accuracy } : {}),
    });
  }
}
