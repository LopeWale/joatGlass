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

  it('adopts the Even host storage value over a stale local mirror', async () => {
    const data = stubStorage({ 'joat.settings.v1': JSON.stringify({ resume: 'confirm' }) });
    const s = new Settings();
    s.load();
    const seen: string[] = [];
    s.onChange((policy) => seen.push(policy));
    await s.attachHostStorage({
      get: async () => JSON.stringify({ resume: 'auto' }),
      set: async () => true,
    });
    expect(s.resumePolicy).toBe('auto');
    expect(seen).toEqual(['auto']);
    // The local mirror converges on the durable hosted value.
    expect(data['joat.settings.v1']).toContain('auto');
  });

  it('never lets a hosted echo override a policy chosen this session', async () => {
    stubStorage();
    const hosted: Record<string, string> = {
      'joat.settings.v1': JSON.stringify({ resume: 'auto' }),
    };
    const s = new Settings();
    s.setResumePolicy('mac');
    await s.attachHostStorage({
      get: async () => hosted['joat.settings.v1'] ?? '',
      set: async (key, value) => {
        hosted[key] = value;
        return true;
      },
    });
    expect(s.resumePolicy).toBe('mac');
    // The host converges on the live choice, so the next boot cannot
    // resurrect the stale hosted policy over the fresher mirror.
    await Promise.resolve();
    expect(hosted['joat.settings.v1']).toContain('mac');
  });

  it('write-throughs every change to the host store, and survives host failures', async () => {
    stubStorage();
    const hosted: Record<string, string> = {};
    const s = new Settings();
    await s.attachHostStorage({
      get: async () => '',
      set: async (key, value) => {
        hosted[key] = value;
        return true;
      },
    });
    s.setResumePolicy('auto');
    await Promise.resolve();
    expect(hosted['joat.settings.v1']).toContain('auto');

    const failing = new Settings();
    await failing.attachHostStorage({
      get: async () => { throw new Error('host offline'); },
      set: async () => { throw new Error('host offline'); },
    });
    failing.setResumePolicy('mac'); // must not throw or reject unhandled
    expect(failing.resumePolicy).toBe('mac');
  });

  it('ignores malformed or invalid hosted values', async () => {
    stubStorage();
    const s = new Settings();
    await s.attachHostStorage({ get: async () => 'not json{{', set: async () => true });
    expect(s.resumePolicy).toBe('confirm');
    const t = new Settings();
    await t.attachHostStorage({
      get: async () => JSON.stringify({ resume: 'yolo' }),
      set: async () => true,
    });
    expect(t.resumePolicy).toBe('confirm');
  });
});
