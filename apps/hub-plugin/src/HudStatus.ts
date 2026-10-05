import type { LinkState } from './MacLink';

/**
 * The status facts the PHONE owns, as data.
 *
 * Until 2026-10-05 Chrome composed "H:MM JOAT <state> bat NN%+" into one
 * string, the renderer regex-parsed it back apart, and the native layouts
 * showed the string verbatim. Two things were lost on the way: the charging
 * mark (the parser stripped it before the spine could draw it) and the
 * battery LEVEL (the spine drew the same full icon at 100% and at 12%).
 * One model, written once by Chrome and read by both the raster spine and
 * the firmware text header, is the fix for both.
 */
export interface HudStatusModel {
  /** "9:41" — the phone's own clock, never the Mac's. */
  clock: string;
  link: LinkState;
  /** Structured microphone truth (audio state), never inferred from words. */
  mic: boolean;
  /** 0–100, absent until the host has reported it. */
  battery?: number;
  charging: boolean;
  /** The Mac's transient state word ("hearing you…"), already stripped of
   * any leading "JOAT". Absent at rest. */
  macState?: string;
}

/** DeviceMonitor's warning threshold; the header also prints the number
 * from here down, so a wearer sees "▁ 12%" and not only a short bar. */
export const HUD_LOW_BATTERY_PCT = 15;

/** A sanity bound on the spine's word. The real fit is by pixels: the
 * raster spine measures it into SPINE_LABEL_BOX and the firmware layouts
 * shorten it with HudPageBuilder.fitHeader, which cuts the word and never
 * the clock or the glyphs at the other end of the line. Planning's own
 * titles ("PLANNING · PLAN · AUG 31–SEP 6") must survive this cap. */
const LABEL_MAX_CHARS = 40;

const BATTERY_BLOCKS = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'] as const;

function upper(value: string): string {
  return value.replace(/\s+/g, ' ').trim().toUpperCase();
}

/**
 * The spine's word. A degraded link outranks everything (a wearer whose
 * brain is gone needs to know that, not what page they are on); the Mac's
 * transient word comes next; at rest it is the page's name, or JOAT at home.
 */
export function hudStatusLabel(model: HudStatusModel, pageTitle?: string): string {
  let label: string;
  if (model.link !== 'connected') label = `JOAT · ${upper(model.link)}`;
  else if (model.macState?.trim()) label = `JOAT · ${upper(model.macState)}`;
  else {
    const title = pageTitle?.trim();
    label = title ? upper(title) : 'JOAT';
  }
  return label.length <= LABEL_MAX_CHARS
    ? label
    : `${label.slice(0, LABEL_MAX_CHARS - 1).trimEnd()}…`;
}

/** ● while the glasses listen, ○ while they do not: the one fact the wearer
 * must be able to read in a glance on every page. Both glyphs are in the
 * firmware font (even-g2-notes glyph tables, U+25CF / U+25CB). */
export function micGlyph(mic: boolean): string {
  return mic ? '●' : '○';
}

/**
 * The battery as one firmware glyph: the eight lower-block characters give
 * an eight-step gauge the font actually has (there is no battery glyph in
 * it). "+" marks charging. From the low-battery threshold down the number
 * returns beside the bar, because a short bar alone is easy to miss.
 */
export function batteryGlyph(level: number | undefined, charging = false): string {
  if (level === undefined || !Number.isFinite(level)) return '';
  const clamped = Math.max(0, Math.min(100, Math.round(level)));
  const index = Math.max(0, Math.min(BATTERY_BLOCKS.length - 1,
    Math.round((clamped / 100) * BATTERY_BLOCKS.length) - 1));
  const block = BATTERY_BLOCKS[index]!;
  if (charging) return `${block}+`;
  return clamped <= HUD_LOW_BATTERY_PCT ? `${block} ${clamped}%` : block;
}

/**
 * The one-line firmware header: clock · label · mic · battery, in the same
 * order the raster spine lays them out left to right. On native pages this
 * replaces the two lines they used to spend (a status line in words plus a
 * separate title line), which is where every firmware list's fifth row
 * came from.
 */
export function nativeStatusLine(model: HudStatusModel, pageTitle?: string): string {
  const parts = [model.clock || '--:--', hudStatusLabel(model, pageTitle), micGlyph(model.mic)];
  const battery = batteryGlyph(model.battery, model.charging);
  if (battery) parts.push(battery);
  return parts.join(' · ');
}

/**
 * Compatibility: a model from the legacy composed line ("9:41 JOAT hearing
 * you… bat 87%+"). Production always carries the model; this serves frames
 * built before Chrome has started and the tests that still write the line.
 */
export function statusModelFromText(text: string, mic: boolean): HudStatusModel {
  const compact = text.replace(/\s+/g, ' ').trim();
  const clock = compact.match(/^\d{1,2}:\d{2}/)?.[0] ?? '';
  const battery = compact.match(/bat\s*(\d+)%(\+?)/i);
  let state = compact
    .replace(/^\d{1,2}:\d{2}\s*/, '')
    .replace(/\s*bat\s*\d+%\+?$/i, '')
    .trim()
    .replace(/^JOAT\s*[·-]?\s*/i, '')
    .trim();
  const linkWord = state.toLowerCase();
  const link: LinkState = linkWord === 'connecting' || linkWord === 'reconnecting' || linkWord === 'closed'
    ? linkWord
    : 'connected';
  if (link !== 'connected' || /^(ready|listening|mic off)$/i.test(state)) state = '';
  return {
    clock,
    link,
    mic,
    ...(battery ? { battery: Number(battery[1]) } : {}),
    charging: battery?.[2] === '+',
    ...(state ? { macState: state } : {}),
  };
}
