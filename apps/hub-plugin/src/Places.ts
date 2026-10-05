import type { SettingsHostStorage } from './Settings';
import type { SavedPlaceWire } from './types';

const STORAGE_KEY = 'joat.places.v1';

export const PLACE_NAME_MAX_CHARS = 40;
export const PLACE_ADDRESS_MAX_CHARS = 120;
export const PLACES_MAX = 50;

/**
 * One place the wearer saved on the phone page: a name, and an address to
 * search for, coordinates to route to, or both. The Mac gets the whole
 * list on every change (`places` uplink) and keeps its own copy, so
 * "navigate home" resolves there without a provider search when the place
 * has coordinates, or searches the saved address when that is all it has.
 */
export interface SavedPlace {
  id: string;
  name: string;
  address?: string;
  latitude?: number;
  longitude?: number;
}

export type SavedPlaceInput = Omit<SavedPlace, 'id'>;

function clean(value: unknown, max: number): string | undefined {
  if (typeof value !== 'string') return undefined;
  const compact = value.replace(/\s+/g, ' ').trim();
  if (!compact) return undefined;
  return compact.length > max ? compact.slice(0, max) : compact;
}

function coordinate(value: unknown, limit: number): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= limit
    ? value
    : undefined;
}

/** A place as the wire and the store accept it, or undefined when it is not one. */
export function normalisePlace(raw: unknown, id?: string): SavedPlace | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined;
  const value = raw as Record<string, unknown>;
  const placeId = id ?? (typeof value.id === 'string' ? value.id : undefined);
  const name = clean(value.name, PLACE_NAME_MAX_CHARS);
  if (!placeId || placeId.length > 64 || !name) return undefined;
  const address = clean(value.address, PLACE_ADDRESS_MAX_CHARS);
  const latitude = coordinate(value.latitude, 90);
  const longitude = coordinate(value.longitude, 180);
  const located = latitude !== undefined && longitude !== undefined;
  if (!address && !located) return undefined;
  return {
    id: placeId,
    name,
    ...(address ? { address } : {}),
    ...(located ? { latitude, longitude } : {}),
  };
}

export function placesToWire(places: readonly SavedPlace[]): SavedPlaceWire[] {
  return places.map((place) => ({
    id: place.id,
    name: place.name,
    ...(place.address ? { address: place.address } : {}),
    ...(place.latitude !== undefined && place.longitude !== undefined
      ? { latitude: place.latitude, longitude: place.longitude }
      : {}),
  }));
}

function newId(): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `place-${random}`;
}

/**
 * The saved places, persisted like Settings: the browser's localStorage is a
 * fast local mirror, the Even host's storage (attachHostStorage) is the copy
 * that survives Android WebView kills and the dev/packed origin switch.
 */
export class Places {
  private places: SavedPlace[] = [];
  private listener?: (places: readonly SavedPlace[]) => void;
  private host?: SettingsHostStorage;
  private changedThisSession = false;

  onChange(listener: (places: readonly SavedPlace[]) => void): void {
    this.listener = listener;
  }

  list(): readonly SavedPlace[] {
    return this.places;
  }

  load(): void {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) this.places = Places.parse(raw);
    } catch {
      // Unreadable storage → no places yet.
    }
  }

  async attachHostStorage(host: SettingsHostStorage): Promise<void> {
    this.host = host;
    let raw: string;
    try {
      raw = await host.get(STORAGE_KEY);
    } catch {
      return;
    }
    if (this.changedThisSession) {
      void host.set(STORAGE_KEY, this.encode()).then(() => undefined, () => undefined);
      return;
    }
    if (!raw) return;
    const hosted = Places.parse(raw);
    if (JSON.stringify(hosted) === JSON.stringify(this.places)) return;
    this.places = hosted;
    this.mirror();
    this.listener?.(this.places);
  }

  add(input: SavedPlaceInput): SavedPlace | undefined {
    if (this.places.length >= PLACES_MAX) return undefined;
    const place = normalisePlace(input, newId());
    if (!place) return undefined;
    this.commit([...this.places, place]);
    return place;
  }

  update(id: string, input: SavedPlaceInput): SavedPlace | undefined {
    const index = this.places.findIndex((place) => place.id === id);
    if (index < 0) return undefined;
    const place = normalisePlace(input, id);
    if (!place) return undefined;
    const next = [...this.places];
    next[index] = place;
    this.commit(next);
    return place;
  }

  remove(id: string): boolean {
    const next = this.places.filter((place) => place.id !== id);
    if (next.length === this.places.length) return false;
    this.commit(next);
    return true;
  }

  private commit(next: SavedPlace[]): void {
    this.places = next;
    this.changedThisSession = true;
    this.mirror();
    void this.host?.set(STORAGE_KEY, this.encode()).then(() => undefined, () => undefined);
    this.listener?.(this.places);
  }

  private mirror(): void {
    try {
      localStorage.setItem(STORAGE_KEY, this.encode());
    } catch {
      // The local mirror is best-effort.
    }
  }

  private encode(): string {
    return JSON.stringify({ places: placesToWire(this.places) });
  }

  private static parse(raw: string): SavedPlace[] {
    try {
      const parsed: unknown = JSON.parse(raw);
      const list = (parsed as { places?: unknown }).places;
      if (!Array.isArray(list)) return [];
      const seen = new Set<string>();
      const places: SavedPlace[] = [];
      for (const item of list) {
        const place = normalisePlace(item);
        if (!place || seen.has(place.id)) continue;
        seen.add(place.id);
        places.push(place);
        if (places.length >= PLACES_MAX) break;
      }
      return places;
    } catch {
      return [];
    }
  }
}
