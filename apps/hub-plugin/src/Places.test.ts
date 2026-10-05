import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PLACES_MAX, Places, normalisePlace, placesToWire } from './Places';

/**
 * The wearer's saved places: edited on the phone page, mirrored to browser
 * storage, written through to the Even host's storage, and sent whole to
 * the Mac on every change. These pin the model; the page is a thin view.
 */
function fakeStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => { store.set(key, value); },
    removeItem: (key: string) => { store.delete(key); },
    clear: () => store.clear(),
    key: () => null,
    get length() { return store.size; },
  } as Storage;
}

beforeEach(() => { vi.stubGlobal('localStorage', fakeStorage()); });
afterEach(() => { vi.unstubAllGlobals(); });

describe('a saved place', () => {
  it('needs a name and an address or a pin, within the wire bounds', () => {
    expect(normalisePlace({ name: '  Home ', address: ' 12 Oak St,  Norman ' }, 'p1'))
      .toEqual({ id: 'p1', name: 'Home', address: '12 Oak St, Norman' });
    expect(normalisePlace({ name: 'Work', latitude: 35.2, longitude: -97.4 }, 'p2'))
      .toEqual({ id: 'p2', name: 'Work', latitude: 35.2, longitude: -97.4 });
    expect(normalisePlace({ name: 'Nowhere' }, 'p3')).toBeUndefined();
    expect(normalisePlace({ name: '', address: 'x' }, 'p4')).toBeUndefined();
    // A pin needs both halves, within range.
    expect(normalisePlace({ name: 'Half', latitude: 35.2 }, 'p5')).toBeUndefined();
    expect(normalisePlace({ name: 'Off', latitude: 95, longitude: 0 }, 'p6')).toBeUndefined();
    expect(normalisePlace({ name: 'x'.repeat(60), address: 'a' }, 'p7')?.name).toHaveLength(40);
  });
});

describe('the places list', () => {
  it('adds, edits and removes, mirrors to storage and announces every change', () => {
    const places = new Places();
    const seen: number[] = [];
    places.onChange((list) => seen.push(list.length));
    const home = places.add({ name: 'Home', address: '12 Oak St' });
    expect(home?.id).toMatch(/^place-/);
    expect(places.add({ name: 'Nowhere' })).toBeUndefined();
    const work = places.add({ name: 'Work', latitude: 35.2, longitude: -97.4 });
    expect(places.update(home!.id, { name: 'Home', address: '14 Oak St' })?.address).toBe('14 Oak St');
    expect(places.update('missing', { name: 'x', address: 'y' })).toBeUndefined();
    expect(places.remove(work!.id)).toBe(true);
    expect(places.remove(work!.id)).toBe(false);
    expect(seen).toEqual([1, 2, 2, 1]);

    // The mirror reloads in the next session.
    const again = new Places();
    again.load();
    expect(again.list()).toEqual([{ id: home!.id, name: 'Home', address: '14 Oak St' }]);
    expect(placesToWire(again.list())).toEqual([{ id: home!.id, name: 'Home', address: '14 Oak St' }]);
  });

  it('adopts the hosted copy unless this session already changed the list', async () => {
    const hosted = { places: [{ id: 'p1', name: 'Home', address: '12 Oak St' }, { id: 'p1', name: 'Dup', address: 'x' }, { name: 'Bad' }] };
    const host = { get: vi.fn(async () => JSON.stringify(hosted)), set: vi.fn(async () => true) };
    const places = new Places();
    const seen: number[] = [];
    places.onChange((list) => seen.push(list.length));
    await places.attachHostStorage(host);
    // Duplicates and malformed entries are dropped; the rest is adopted.
    expect(places.list()).toEqual([{ id: 'p1', name: 'Home', address: '12 Oak St' }]);
    expect(seen).toEqual([1]);

    const live = new Places();
    live.add({ name: 'Work', address: 'Campus' });
    const liveHost = { get: vi.fn(async () => JSON.stringify(hosted)), set: vi.fn(async () => true) };
    await live.attachHostStorage(liveHost);
    expect(live.list().map((place) => place.name)).toEqual(['Work']);
    // The live choice is written through so the host converges on it.
    expect(liveHost.set).toHaveBeenCalledWith('joat.places.v1', expect.stringContaining('Work'));
  });

  it('caps the list at the wire maximum', () => {
    const places = new Places();
    for (let index = 0; index < PLACES_MAX; index += 1) {
      expect(places.add({ name: `Place ${index}`, address: 'x' })).toBeDefined();
    }
    expect(places.add({ name: 'One more', address: 'x' })).toBeUndefined();
    expect(places.list()).toHaveLength(PLACES_MAX);
  });
});
