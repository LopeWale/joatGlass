import { afterEach, describe, expect, it } from 'vitest';
import { Settings } from './Settings';

/** Minimal localStorage stand-in — vitest runs in node, which has none. */
function stubStorage(initial: Record<string, string> = {}): Record<string, string> {
  const data = { ...initial };
  (globalThis as { localStorage?: unknown }).localStorage = {
    getItem: (k: string) => (k in data ? data[k] : null),
    setItem: (k: string, v: string) => {
      data[k] = v;
    },
  };
  return data;
}

afterEach(() => {
  delete (globalThis as { localStorage?: unknown }).localStorage;
});

describe('Settings', () => {
  it('defaults to confirm — the fail-safe resume policy', () => {
    expect(new Settings().resumePolicy).toBe('confirm');
  });

  it('survives absent storage (node, private mode): load and set never throw', () => {
    const s = new Settings();
    s.load();
    s.setResumePolicy('auto');
    expect(s.resumePolicy).toBe('auto'); // holds for the session
  });

  it('round-trips through storage', () => {
    const data = stubStorage();
    const a = new Settings();
    a.setResumePolicy('mac');
    expect(data['joat.settings.v1']).toContain('mac');

    const b = new Settings();
    b.load();
    expect(b.resumePolicy).toBe('mac');
  });

  it('rejects garbage JSON and unknown policy values from storage', () => {
    stubStorage({ 'joat.settings.v1': 'not json{{' });
    const a = new Settings();
    a.load();
    expect(a.resumePolicy).toBe('confirm');

    stubStorage({ 'joat.settings.v1': JSON.stringify({ resume: 'yolo' }) });
    const b = new Settings();
    b.load();
    expect(b.resumePolicy).toBe('confirm');
  });
});
