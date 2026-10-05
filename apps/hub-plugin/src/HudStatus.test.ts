import { describe, expect, it } from 'vitest';
import {
  HUD_LOW_BATTERY_PCT,
  type HudStatusModel,
  batteryGlyph,
  hudStatusLabel,
  micGlyph,
  nativeStatusLine,
  statusModelFromText,
} from './HudStatus';

const rest: HudStatusModel = { clock: '9:41', link: 'connected', mic: false, battery: 87, charging: false };

describe('the spine word', () => {
  it('is the page at rest, JOAT at home, and never the microphone', () => {
    expect(hudStatusLabel(rest, 'NEWS')).toBe('NEWS');
    expect(hudStatusLabel(rest)).toBe('JOAT');
    // The mic used to turn "NEWS" into "JOAT · LISTENING"; the glyph and
    // icon carry it now, so a page keeps its name while the wearer talks.
    expect(hudStatusLabel({ ...rest, mic: true }, 'NEWS')).toBe('NEWS');
  });

  it('lets a degraded link outrank the page and the Mac outrank rest', () => {
    expect(hudStatusLabel({ ...rest, link: 'reconnecting' }, 'NEWS')).toBe('JOAT · RECONNECTING');
    expect(hudStatusLabel({ ...rest, macState: 'hearing you…' }, 'NEWS')).toBe('JOAT · HEARING YOU…');
    expect(hudStatusLabel({ ...rest, link: 'closed', macState: 'hearing you…' })).toBe('JOAT · CLOSED');
  });

  it('is bounded, and wide enough for Planning\'s own titles', () => {
    const label = hudStatusLabel({ ...rest, macState: 'brainstorm connection test preparing reconnect' });
    expect(label).toBe('JOAT · BRAINSTORM CONNECTION TEST PREPA…');
    expect(label.length).toBeLessThanOrEqual(40);
    expect(hudStatusLabel(rest, 'PLANNING · PLAN · AUG 31–SEP 6')).toBe('PLANNING · PLAN · AUG 31–SEP 6');
  });
});

describe('the glyphs', () => {
  it('spell the microphone as a filled or empty dot', () => {
    expect(micGlyph(true)).toBe('●');
    expect(micGlyph(false)).toBe('○');
  });

  it('spell the battery as an eight-step block, a charge mark, and the number only when low', () => {
    expect(batteryGlyph(100)).toBe('█');
    expect(batteryGlyph(87)).toBe('▇');
    expect(batteryGlyph(80)).toBe('▆');
    expect(batteryGlyph(50)).toBe('▄');
    expect(batteryGlyph(HUD_LOW_BATTERY_PCT + 1)).toBe('▁');
    expect(batteryGlyph(HUD_LOW_BATTERY_PCT)).toBe(`▁ ${HUD_LOW_BATTERY_PCT}%`);
    expect(batteryGlyph(3)).toBe('▁ 3%');
    expect(batteryGlyph(0)).toBe('▁ 0%');
    // Charging: the mark, and no low-battery number while it climbs.
    expect(batteryGlyph(12, true)).toBe('▁+');
    expect(batteryGlyph(100, true)).toBe('█+');
    // Unknown until the host reports it.
    expect(batteryGlyph(undefined)).toBe('');
    expect(batteryGlyph(Number.NaN)).toBe('');
  });
});

describe('the one-line firmware header', () => {
  it('reads clock · page · mic · battery, in the spine\'s own order', () => {
    expect(nativeStatusLine(rest, 'POMODORO')).toBe('9:41 · POMODORO · ○ · ▇');
    expect(nativeStatusLine({ ...rest, mic: true, macState: 'hearing you…' }, 'POMODORO'))
      .toBe('9:41 · JOAT · HEARING YOU… · ● · ▇');
    expect(nativeStatusLine({ ...rest, link: 'reconnecting', battery: 12 }))
      .toBe('9:41 · JOAT · RECONNECTING · ○ · ▁ 12%');
  });

  it('drops the battery segment until the host has reported a level', () => {
    const { battery: _omit, ...unknown } = rest;
    expect(nativeStatusLine(unknown, 'NEWS')).toBe('9:41 · NEWS · ○');
    expect(nativeStatusLine({ ...unknown, clock: '' })).toBe('--:-- · JOAT · ○');
  });
});

describe('the legacy line, parsed back into a model', () => {
  it('recovers clock, battery, charging and the Mac\'s word', () => {
    expect(statusModelFromText('9:41 JOAT hearing you… bat 87%+', true)).toEqual({
      clock: '9:41', link: 'connected', mic: true, battery: 87, charging: true, macState: 'hearing you…',
    });
  });

  it('reads a link word as the link and resting words as rest', () => {
    expect(statusModelFromText('9:41 JOAT reconnecting', false)).toMatchObject({ link: 'reconnecting' });
    expect(statusModelFromText('9:41 JOAT reconnecting', false).macState).toBeUndefined();
    for (const word of ['ready', 'listening', 'mic off']) {
      const model = statusModelFromText(`9:41 JOAT ${word} bat 50%`, false);
      expect(model.macState).toBeUndefined();
      expect(model).toMatchObject({ link: 'connected', battery: 50, charging: false });
    }
    expect(statusModelFromText('', false)).toEqual({ clock: '', link: 'connected', mic: false, charging: false });
  });
});
