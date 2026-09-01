import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  captureBackgroundSnapshotForTest,
  onBackgroundRestore,
  resetBackgroundStateForTest,
  setBackgroundState,
} from './BackgroundState';

/**
 * Backgrounding the phone is not a pause: the host snapshots this page, builds
 * a NEW headless WebView, reloads the same URL, and replays the snapshot. A
 * plugin that registers nothing gets '{}' back and reboots to its initial
 * state on every pocket-and-return.
 */

interface HostGlobals {
  __getStateSnapshot?: () => string;
  __restoreState?: (snapshot: string | Record<string, unknown>) => void;
}

const host = () => globalThis as unknown as HostGlobals;

afterEach(() => resetBackgroundStateForTest());

describe('the host state bridge', () => {
  it('publishes the two globals the host actually calls', () => {
    expect(host().__getStateSnapshot).toBeUndefined();
    setBackgroundState('k', () => ({ a: 1 }));
    expect(typeof host().__getStateSnapshot).toBe('function');
    expect(typeof host().__restoreState).toBe('function');
  });

  it('carries state across the JSON boundary the host puts between pages', () => {
    let counter = 7;
    let page = 'planning';
    setBackgroundState('joat', () => ({ counter, page }));

    // The host serializes here, then a DIFFERENT page deserializes.
    const wire = host().__getStateSnapshot!();
    expect(typeof wire).toBe('string');

    counter = 0;
    page = 'overview';
    onBackgroundRestore('joat', (saved) => {
      counter = (saved.counter as number) ?? counter;
      page = (saved.page as string) ?? page;
    });
    host().__restoreState!(wire);

    expect(counter).toBe(7);
    expect(page).toBe('planning');
  });

  it('accepts the snapshot as an already-parsed object too', () => {
    let value = 'lost';
    onBackgroundRestore('joat', (saved) => { value = saved.value as string; });
    host().__restoreState!({ joat: { value: 'kept' } });
    expect(value).toBe('kept');
  });

  it('returns an empty snapshot rather than throwing when nothing is registered', () => {
    setBackgroundState('k', () => ({}));
    resetBackgroundStateForTest();
    expect(captureBackgroundSnapshotForTest()).toBe('{}');
  });

  it('one failing exporter cannot cost the wearer every other slice', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    setBackgroundState('bad', () => { throw new Error('boom'); });
    setBackgroundState('good', () => ({ kept: true }));

    const parsed = JSON.parse(captureBackgroundSnapshotForTest());
    expect(parsed.good).toEqual({ kept: true });
    expect(parsed.bad).toBeUndefined();
    error.mockRestore();
  });

  it('a corrupt snapshot is skipped, not trusted', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    let value = 'untouched';
    onBackgroundRestore('joat', (saved) => { value = saved.value as string; });

    host().__restoreState!('not json{{');
    expect(value).toBe('untouched');

    // A missing or non-object slice is skipped rather than applied.
    host().__restoreState!('{"joat": "a string, not an object"}');
    expect(value).toBe('untouched');
    error.mockRestore();
  });

  it('a throwing restorer cannot break the page it is restoring into', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    let reached = false;
    onBackgroundRestore('bad', () => { throw new Error('boom'); });
    onBackgroundRestore('good', () => { reached = true; });
    expect(() => host().__restoreState!({ bad: {}, good: {} })).not.toThrow();
    expect(reached).toBe(true);
    error.mockRestore();
  });

  it('drops values that cannot survive a JSON round trip', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    setBackgroundState('cyclic', () => cyclic);
    // Serialization fails for the whole snapshot; report empty, never throw.
    expect(captureBackgroundSnapshotForTest()).toBe('{}');
    error.mockRestore();
  });
});
