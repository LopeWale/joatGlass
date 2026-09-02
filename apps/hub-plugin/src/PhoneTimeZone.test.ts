import { afterEach, describe, expect, it, vi } from 'vitest';
import { phoneTimeZone } from './PhoneTimeZone';

/** Replace what the WebView reports as its zone, however it misbehaves. */
function hostReports(timeZone: unknown): void {
  vi.spyOn(Intl, 'DateTimeFormat').mockImplementation(
    () => ({ resolvedOptions: () => ({ timeZone }) }) as unknown as Intl.DateTimeFormat,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('phoneTimeZone', () => {
  it('reports the host WebView’s IANA zone', () => {
    hostReports('America/Chicago');
    expect(phoneTimeZone()).toBe('America/Chicago');
  });

  it('accepts the three-segment and legacy bare names tzdata really has', () => {
    hostReports('America/Argentina/Buenos_Aires');
    expect(phoneTimeZone()).toBe('America/Argentina/Buenos_Aires');
    hostReports('UTC');
    expect(phoneTimeZone()).toBe('UTC');
    hostReports('EST5EDT');
    expect(phoneTimeZone()).toBe('EST5EDT');
  });

  it('trims, because a padded name is still a name', () => {
    hostReports('  Europe/London  ');
    expect(phoneTimeZone()).toBe('Europe/London');
  });

  // A WRONG zone is worse than none: it moves every event time by whole
  // hours while looking entirely plausible. Every unusable answer must
  // degrade to "no opinion", which leaves the Mac on its own zone.
  it.each([
    ['nothing at all', undefined],
    ['an empty string', ''],
    ['whitespace', '   '],
    ['a non-string', 42],
    ['a sentence', 'Not A Zone'],
    ['a traversal', '../../etc/passwd'],
    ['four segments', 'A/B/C/D'],
    ['an over-long name', `Europe/${'x'.repeat(64)}`],
  ])('returns undefined for %s', (_label, value) => {
    hostReports(value);
    expect(phoneTimeZone()).toBeUndefined();
  });

  it('never throws when the host has no working Intl', () => {
    vi.spyOn(Intl, 'DateTimeFormat').mockImplementation(() => {
      throw new Error('no Intl in this WebView');
    });
    expect(() => phoneTimeZone()).not.toThrow();
    expect(phoneTimeZone()).toBeUndefined();
  });
});
