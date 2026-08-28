import chatCircleSvg from '@phosphor-icons/core/regular/chat-circle-dots.svg?raw';
import bellSvg from '@phosphor-icons/core/regular/bell.svg?raw';
import calendarSvg from '@phosphor-icons/core/regular/calendar-dots.svg?raw';
import scalesSvg from '@phosphor-icons/core/regular/scales.svg?raw';
import gearSvg from '@phosphor-icons/core/regular/gear.svg?raw';
import microphoneSvg from '@phosphor-icons/core/regular/microphone.svg?raw';
import batteryHighSvg from '@phosphor-icons/core/regular/battery-high.svg?raw';
import arrowRightSvg from '@phosphor-icons/core/regular/arrow-right.svg?raw';
import arrowLeftSvg from '@phosphor-icons/core/regular/arrow-left.svg?raw';
import { CANVAS } from './types';
import {
  splitGray4Tiles,
  type HudFrameRenderer,
  type HudImageTile,
  type HudVisualFrame,
} from './HudVisualSurface';

const BRIGHT = 238;
const SUPPORT = 170;
const CHROME = 102;
const QUIET = 68;
const HAIRLINE = 51;
const HUD_FONT = '"SF Mono", SFMono-Regular, Menlo, monospace';

/** Measured from the selected 576×288 reference panels. Keeping the major
 * anchors together makes visual drift obvious in review and in tests. */
export const HUD_VISUAL_GEOMETRY = {
  statusBaselineY: 20,
  statusDividerY: 35.5,
  overviewMainX: 176,
  overviewTrayY: 168,
  overviewContextY: 214,
  sidebarWidth: 160,
  sidebarBackdropOffsetX: 78,
  listContentX: 93,
  confirmationActionX: 194,
  confirmationProgressX: 97,
  confirmationProgressWidth: 367,
} as const;

type IconName =
  | 'messages'
  | 'notifications'
  | 'calendar'
  | 'debate'
  | 'settings'
  | 'microphone'
  | 'battery'
  | 'arrowRight'
  | 'arrowLeft';

const ICON_SOURCES: Readonly<Record<IconName, string>> = {
  messages: chatCircleSvg,
  notifications: bellSvg,
  calendar: calendarSvg,
  debate: scalesSvg,
  settings: gearSvg,
  microphone: microphoneSvg,
  battery: batteryHighSvg,
  arrowRight: arrowRightSvg,
  arrowLeft: arrowLeftSvg,
};

interface StatusParts {
  time: string;
  state: string;
  battery?: string;
}

function gray(level: number): string {
  const value = Math.max(0, Math.min(255, Math.round(level)));
  return `rgb(${value}, ${value}, ${value})`;
}

/** Convert the SDK's sticky 0...4 textColor contract to the authored 8-bit
 * canvas palette so Mac urgency survives the custom raster renderer. */
export function hudBrightnessGray(level: number): number {
  return [34, QUIET, CHROME, SUPPORT, BRIGHT][Math.max(0, Math.min(4, Math.round(level)))]!;
}

/** The canvas uses normal 8-bit color for antialiasing, but the G2 protocol
 * consumes one unpacked 0...15 grayscale value per pixel. Quantizing here is
 * essential: passing 0...255 clips most authored hierarchy to full green. */
export function gray8ToGray4(value: number): number {
  return Math.max(0, Math.min(15, Math.round(value * 15 / 255)));
}

export function hudDisplayTitle(value: string): string {
  const normalized = value.replace(/\s*·\s*/g, ' · ').trim();
  if (!normalized || normalized !== normalized.toUpperCase()) return normalized;
  return normalized.split(' · ').map((part) => {
    if (part === 'JOAT' || /^\d+$/.test(part)) return part;
    const lower = part.toLowerCase();
    return lower.charAt(0).toUpperCase() + lower.slice(1);
  }).join(' · ');
}

function parseStatus(status: string): StatusParts {
  const compact = status.replace(/\s+/g, ' ').trim();
  const time = compact.match(/^\d{1,2}:\d{2}/)?.[0] ?? '';
  const battery = compact.match(/bat\s*(\d+)%/i)?.[1];
  let state = compact
    .replace(/^\d{1,2}:\d{2}\s*/, '')
    .replace(/\s*bat\s*\d+%\+?$/i, '')
    .trim();
  state = state.replace(/^JOAT\s*[·-]?\s*/i, '').trim();
  return {
    time,
    state: `JOAT · ${(state || 'READY').toUpperCase()}`,
    ...(battery ? { battery } : {}),
  };
}

function loadSvg(source: string): Promise<HTMLImageElement> {
  const image = new Image();
  const svg = source.replace(/currentColor/g, '#ffffff');
  image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  return new Promise((resolve, reject) => {
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('HUD icon asset failed to load'));
  });
}

function roundedRect(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
): void {
  const r = Math.min(radius, width / 2, height / 2);
  context.beginPath();
  context.moveTo(x + r, y);
  context.arcTo(x + width, y, x + width, y + height, r);
  context.arcTo(x + width, y + height, x, y + height, r);
  context.arcTo(x, y + height, x, y, r);
  context.arcTo(x, y, x + width, y, r);
  context.closePath();
}

function splitPair(value: string): [string, string] {
  const [first = '', ...rest] = value.split('\n');
  return [first.trim(), rest.join(' ').trim()];
}

export interface HudLabeledValue {
  label: string;
  value: string;
  structured: boolean;
}

export interface HudOverviewInput {
  status: string;
  main: string;
  context: string;
  tray: string;
}

export type ResolvedHudOverview =
  | {
    kind: 'empty';
    title: string;
    primary: string;
    recovery: string;
    hint: string;
  }
  | {
    kind: 'content';
    main?: HudLabeledValue;
    tray?: string;
    context?: HudLabeledValue;
  };

/** Dashboard fixtures may send `LABEL\nVALUE`, but ordinary Mac replies and
 * transcripts are plain strings. Plain content is the value, never a label. */
export function parseHudLabeledValue(
  input: string,
  fallbackLabel: string,
  emptyValue = '',
): HudLabeledValue {
  const trimmed = input.trim();
  if (!trimmed) return { label: fallbackLabel, value: emptyValue, structured: false };
  if (!input.includes('\n')) {
    return { label: fallbackLabel, value: trimmed, structured: false };
  }
  const [label, value] = splitPair(input);
  return {
    label: label || fallbackLabel,
    value: value || emptyValue,
    structured: true,
  };
}

/** Resolve the Mac-owned Overview regions before drawing. An absent payload
 * is a real transport state, not a reply, schedule, notification, or query.
 * Keeping this pure makes the ordinary (fixture-free) boot state testable. */
export function resolveHudOverview(input: HudOverviewInput): ResolvedHudOverview {
  const main = input.main.trim();
  const context = input.context.trim();
  const tray = input.tray.trim().replace(/\s+waiting$/i, '');
  if (/\bclosed\b|\boffline\b/i.test(input.status)) {
    return {
      kind: 'empty',
      title: 'Offline',
      primary: 'JOAT on your Mac is unavailable.',
      recovery: 'Open JOAT on your Mac to reconnect.',
      hint: 'left x2: menu · long-press: mic',
    };
  }
  if (/\breconnecting\b/i.test(input.status)) {
    return {
      kind: 'empty',
      title: 'Reconnecting',
      primary: 'Restoring JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: 'left x2: menu · long-press: mic',
    };
  }
  if (/\bconnecting\b/i.test(input.status)) {
    return {
      kind: 'empty',
      title: 'Connecting',
      primary: 'Waiting for JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: 'left x2: menu · long-press: mic',
    };
  }
  if (!main && !context && !tray) {
    return {
      kind: 'empty',
      title: 'Ready',
      primary: 'Ask JOAT anything.',
      recovery: 'Say “JOAT …” or double-click left for the menu.',
      hint: 'left x2: menu · long-press: mic',
    };
  }

  return {
    kind: 'content',
    ...(main ? { main: parseHudLabeledValue(main, 'REPLY') } : {}),
    ...(tray ? { tray } : {}),
    ...(context ? { context: parseHudLabeledValue(context, 'LAST QUERY') } : {}),
  };
}

function splitMessage(value: string): [string, string] {
  const marker = value.indexOf('·');
  if (marker < 0) return [value.trim(), ''];
  return [value.slice(0, marker).trim(), value.slice(marker + 1).trim()];
}

export interface HudListItemParts {
  primary: string;
  secondary: string;
  age: string;
}

/** The Mac prefixes real relative ages as `[2m]`; fixtures and empty/setup
 * rows omit the prefix, so the renderer never invents a timestamp. */
export function parseHudListItem(value: string): HudListItemParts {
  const ageMatch = value.match(/^\[([^\]]+)]\s*/);
  const content = ageMatch ? value.slice(ageMatch[0].length) : value;
  const [primary, secondary] = splitMessage(content);
  return { primary, secondary, age: ageMatch?.[1]?.trim() ?? '' };
}

export interface HudVisibleListItem {
  item: string;
  index: number;
}

/** Wrap every character into measured lines. Long unbroken values are split
 * instead of clipped, which matters for file names and confirmation targets. */
export function wrapHudText(
  text: string,
  maxWidth: number,
  measure: (value: string) => number,
): string[] {
  const words = text.trim().split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = '';

  const pushLongWord = (word: string): string => {
    let remainder = word;
    while (remainder && measure(remainder) > maxWidth) {
      let end = remainder.length;
      while (end > 1 && measure(remainder.slice(0, end)) > maxWidth) end -= 1;
      lines.push(remainder.slice(0, end));
      remainder = remainder.slice(end);
    }
    return remainder;
  };

  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (measure(candidate) <= maxWidth) {
      line = candidate;
      continue;
    }
    if (line) {
      lines.push(line);
      line = '';
    }
    line = pushLongWord(word);
  }
  if (line) lines.push(line);
  return lines;
}

/** Streaming replies show the newest complete lines. The opening words have
 * already been read; pinning them would make every later token invisible. */
export function visibleHudTailLines(
  lines: readonly string[],
  maximumLines: number,
): string[] {
  const count = Math.max(1, maximumLines);
  const visible = lines.slice(-count);
  if (lines.length > count && visible.length > 0) visible[0] = `…${visible[0]}`;
  return visible;
}

/** Keep the selected row visible on the three-row optical page without
 * throwing away rows four and five from the Mac's capability results. */
export function visibleHudListItems(
  items: readonly string[],
  selectedIndex: number,
  capacity = 3,
): HudVisibleListItem[] {
  const count = Math.max(1, capacity);
  const maximumStart = Math.max(0, items.length - count);
  const start = Math.min(Math.max(0, selectedIndex - count + 1), maximumStart);
  return items.slice(start, start + count).map((item, offset) => ({
    item,
    index: start + offset,
  }));
}

export class HudCanvasRenderer implements HudFrameRenderer {
  private readonly icons: Promise<Record<IconName, HTMLImageElement>>;

  constructor() {
    this.icons = Promise.all(
      (Object.entries(ICON_SOURCES) as Array<[IconName, string]>)
        .map(async ([name, source]) => [name, await loadSvg(source)] as const),
    ).then((entries) => Object.fromEntries(entries) as Record<IconName, HTMLImageElement>);
  }

  async render(frame: HudVisualFrame): Promise<readonly HudImageTile[]> {
    const icons = await this.icons;
    const canvas = document.createElement('canvas');
    canvas.width = CANVAS.width;
    canvas.height = CANVAS.height;
    const context = canvas.getContext('2d', { alpha: false });
    if (!context) throw new Error('2D canvas is unavailable');

    context.fillStyle = '#000000';
    context.fillRect(0, 0, CANVAS.width, CANVAS.height);
    context.imageSmoothingEnabled = true;
    this.drawShell(context, frame.status, frame.micOn, icons);

    switch (frame.kind) {
      case 'overview':
        this.drawOverview(context, frame, icons);
        break;
      case 'sidebar':
        this.drawSidebar(context, frame, icons);
        break;
      case 'list':
        this.drawList(context, frame);
        break;
      case 'confirmation':
        this.drawConfirmation(context, frame, icons);
        break;
    }

    const rgba = context.getImageData(0, 0, CANVAS.width, CANVAS.height).data;
    const pixels = new Uint8Array(CANVAS.width * CANVAS.height);
    for (let source = 0, target = 0; source < rgba.length; source += 4, target += 1) {
      pixels[target] = gray8ToGray4(
        Math.max(rgba[source] ?? 0, rgba[source + 1] ?? 0, rgba[source + 2] ?? 0),
      );
    }
    return splitGray4Tiles(pixels);
  }

  private drawShell(
    context: CanvasRenderingContext2D,
    statusText: string,
    micOn: boolean,
    icons: Record<IconName, HTMLImageElement>,
  ): void {
    context.strokeStyle = gray(HAIRLINE);
    context.lineWidth = 1;
    roundedRect(context, 1.5, 1.5, 573, 285, 20);
    context.stroke();

    const status = parseStatus(statusText);
    context.textBaseline = 'middle';
    context.fillStyle = gray(SUPPORT);
    context.font = `12px ${HUD_FONT}`;
    context.textAlign = 'left';
    context.fillText(
      status.time || '--:--',
      30,
      HUD_VISUAL_GEOMETRY.statusBaselineY,
    );
    context.textAlign = 'center';
    context.font = `10px ${HUD_FONT}`;
    context.fillText(status.state, 288, HUD_VISUAL_GEOMETRY.statusBaselineY);

    this.drawIcon(context, icons.microphone, 410, 12, 15, micOn ? SUPPORT : QUIET);
    context.textAlign = 'left';
    context.fillText(micOn ? 'ON' : 'OFF', 428, HUD_VISUAL_GEOMETRY.statusBaselineY);
    if (status.battery) {
      context.textAlign = 'right';
      context.fillText(`${status.battery}%`, 531, HUD_VISUAL_GEOMETRY.statusBaselineY);
      this.drawIcon(context, icons.battery, 538, 12, 20, SUPPORT);
    }

    context.strokeStyle = gray(HAIRLINE);
    context.beginPath();
    context.moveTo(17, HUD_VISUAL_GEOMETRY.statusDividerY);
    context.lineTo(280, HUD_VISUAL_GEOMETRY.statusDividerY);
    context.lineTo(284, HUD_VISUAL_GEOMETRY.statusDividerY + 4);
    context.lineTo(292, HUD_VISUAL_GEOMETRY.statusDividerY + 4);
    context.lineTo(296, HUD_VISUAL_GEOMETRY.statusDividerY);
    context.lineTo(559, HUD_VISUAL_GEOMETRY.statusDividerY);
    context.stroke();
  }

  private drawOverview(
    context: CanvasRenderingContext2D,
    frame: {
      status: string;
      main: string;
      mainBrightness: number;
      context: string;
      tray: string;
      hint: string;
    },
    icons: Record<IconName, HTMLImageElement>,
    opacity = 1,
    offsetX = 0,
  ): void {
    context.save();
    context.globalAlpha = opacity;
    context.translate(offsetX, 0);
    const overview = resolveHudOverview(frame);

    if (overview.kind === 'empty') {
      const left = opacity < 1 ? 110 : 36;
      context.textAlign = 'left';
      context.textBaseline = 'middle';
      context.fillStyle = gray(BRIGHT);
      context.font = `20px ${HUD_FONT}`;
      context.fillText(overview.title, left, 64);
      context.fillStyle = gray(SUPPORT);
      context.font = `16px ${HUD_FONT}`;
      context.fillText(overview.primary, left, 106);
      context.fillStyle = gray(CHROME);
      context.font = `10px ${HUD_FONT}`;
      context.fillText(overview.recovery, left, 132);
      context.fillStyle = gray(QUIET);
      context.font = `9px ${HUD_FONT}`;
      context.textAlign = 'center';
      context.fillText(overview.hint, 288 - offsetX, 267);
      context.restore();
      return;
    }

    if (overview.main) {
      const main = overview.main;
      context.textAlign = 'left';
      context.textBaseline = 'middle';
      context.fillStyle = gray(SUPPORT);
      context.font = `10px ${HUD_FONT}`;
      context.fillText(main.label.toUpperCase(), 110, 76);
      const mainIcon = main.structured && /NEXT|CALENDAR/i.test(main.label)
        ? icons.calendar
        : icons.messages;
      const mainLevel = hudBrightnessGray(frame.mainBrightness);
      this.drawIcon(context, mainIcon, 111, 91, 32, mainLevel);
      context.fillStyle = gray(mainLevel);
      const mainSize = opacity < 1 ? 19 : main.structured ? 25 : 18;
      context.font = `${mainSize}px ${HUD_FONT}`;
      if (main.structured || opacity < 1) {
        this.fitText(
          context,
          main.value,
          HUD_VISUAL_GEOMETRY.overviewMainX,
          112,
          opacity < 1 ? 330 : 359,
        );
      } else {
        this.drawWrappedText(
          context,
          main.value,
          HUD_VISUAL_GEOMETRY.overviewMainX,
          103,
          359,
          22,
          2,
        );
      }
    }

    if (overview.tray) {
      this.drawIcon(context, icons.notifications, 112, 158, 19, SUPPORT);
      context.fillStyle = gray(SUPPORT);
      context.font = `14px ${HUD_FONT}`;
      context.fillText(
        overview.tray,
        147,
        HUD_VISUAL_GEOMETRY.overviewTrayY,
      );
    }

    if (overview.context) {
      context.fillStyle = gray(CHROME);
      context.font = `9px ${HUD_FONT}`;
      context.fillText(
        overview.context.label.toUpperCase(),
        110,
        HUD_VISUAL_GEOMETRY.overviewContextY,
      );
      context.fillStyle = gray(SUPPORT);
      context.font = `10px ${HUD_FONT}`;
      this.fitText(
        context,
        overview.context.value,
        205,
        HUD_VISUAL_GEOMETRY.overviewContextY,
        330,
      );
    }

    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText(frame.hint, 288 - offsetX, 263);
    context.restore();
  }

  private drawSidebar(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'sidebar' }>,
    icons: Record<IconName, HTMLImageElement>,
  ): void {
    this.drawOverview(
      context,
      frame,
      icons,
      0.24,
      HUD_VISUAL_GEOMETRY.sidebarBackdropOffsetX,
    );
    context.fillStyle = '#000000';
    context.fillRect(3, 38, HUD_VISUAL_GEOMETRY.sidebarWidth - 3, 246);
    context.strokeStyle = gray(HAIRLINE);
    context.beginPath();
    context.moveTo(1.5, 38);
    context.lineTo(1.5, 267);
    context.stroke();

    context.strokeStyle = gray(17);
    context.beginPath();
    context.moveTo(HUD_VISUAL_GEOMETRY.sidebarWidth + 0.5, 55);
    context.lineTo(HUD_VISUAL_GEOMETRY.sidebarWidth + 0.5, 250);
    context.stroke();

    const iconNames: IconName[] = ['messages', 'notifications', 'calendar', 'debate', 'settings'];
    frame.items.slice(0, 5).forEach((item, index) => {
      const y = 70 + index * 39;
      if (index === frame.selectedIndex) {
        context.fillStyle = '#000000';
        context.fillRect(12, y - 17, HUD_VISUAL_GEOMETRY.sidebarWidth - 14, 34);
        context.fillStyle = gray(BRIGHT);
        context.fillRect(12, y - 17, 3, 34);
      }
      const level = index === frame.selectedIndex ? BRIGHT : SUPPORT;
      const icon = icons[iconNames[index] ?? 'messages'];
      this.drawIcon(context, icon, 26, y - 9, 18, level);
      context.fillStyle = gray(level);
      context.textAlign = 'left';
      context.textBaseline = 'middle';
      context.font = `13px ${HUD_FONT}`;
      context.fillText(item, 54, y);
    });
  }

  private drawList(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'list' }>,
  ): void {
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    context.fillStyle = gray(BRIGHT);
    context.font = `20px ${HUD_FONT}`;
    context.fillText(hudDisplayTitle(frame.title), 36, 64);

    visibleHudListItems(frame.items, frame.selectedIndex).forEach((visible, row) => {
      const { item, index } = visible;
      const { primary: name, secondary: message, age } = parseHudListItem(item);
      const top = 82 + row * 56;
      if (index === frame.selectedIndex) {
        context.strokeStyle = gray(BRIGHT);
        context.lineWidth = 1.2;
        roundedRect(context, 23, top - 8, 527, 48, 5);
        context.stroke();
      } else {
        context.strokeStyle = gray(70);
        context.lineWidth = 0.7;
        context.beginPath();
        context.moveTo(26, top + 40);
        context.lineTo(550, top + 40);
        context.stroke();
      }

      context.strokeStyle = gray(SUPPORT);
      context.lineWidth = 1;
      context.beginPath();
      context.arc(56, top + 16, 14, 0, Math.PI * 2);
      context.stroke();
      context.fillStyle = gray(SUPPORT);
      context.textAlign = 'center';
      context.font = `14px ${HUD_FONT}`;
      context.fillText((name || item).charAt(0).toUpperCase(), 56, top + 16);

      context.textAlign = 'left';
      context.fillStyle = gray(index === frame.selectedIndex ? BRIGHT : SUPPORT);
      context.font = `16px ${HUD_FONT}`;
      context.fillText(name || item, HUD_VISUAL_GEOMETRY.listContentX, top + 8);
      if (message) {
        context.fillStyle = gray(SUPPORT);
        context.font = `10px ${HUD_FONT}`;
        this.fitText(context, message, HUD_VISUAL_GEOMETRY.listContentX, top + 28, 360);
      }
      context.textAlign = 'right';
      context.fillStyle = gray(CHROME);
      context.font = `9px ${HUD_FONT}`;
      context.fillText(age, 520, top + 8);
    });

    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText(frame.hint, 288, 267);
  }

  private drawConfirmation(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'confirmation' }>,
    icons: Record<IconName, HTMLImageElement>,
  ): void {
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillStyle = gray(SUPPORT);
    context.font = `13px ${HUD_FONT}`;
    context.fillText('Confirm action', 288, 79);
    context.strokeStyle = gray(CHROME);
    context.beginPath();
    context.moveTo(274, 96.5);
    context.lineTo(302, 96.5);
    context.stroke();

    context.fillStyle = gray(BRIGHT);
    this.drawFittedWrappedText(context, frame.prompt, 288, 127, 470, 3);

    this.drawIcon(context, icons.arrowRight, 153, 164, 22, BRIGHT);
    context.textAlign = 'left';
    context.fillStyle = gray(BRIGHT);
    context.font = `10px ${HUD_FONT}`;
    context.fillText('RIGHT / RING / NOD', HUD_VISUAL_GEOMETRY.confirmationActionX, 177);
    context.fillStyle = gray(SUPPORT);
    context.fillText('Confirm', 359, 177);

    this.drawIcon(context, icons.arrowLeft, 153, 200, 22, SUPPORT);
    context.fillStyle = gray(SUPPORT);
    context.fillText('LEFT', HUD_VISUAL_GEOMETRY.confirmationActionX, 213);
    context.fillStyle = gray(CHROME);
    context.fillText('Cancel', 247, 213);

    const progress = Math.max(0, Math.min(1, frame.secondsRemaining / 8));
    context.fillStyle = gray(38);
    context.fillRect(
      HUD_VISUAL_GEOMETRY.confirmationProgressX,
      246,
      HUD_VISUAL_GEOMETRY.confirmationProgressWidth,
      3,
    );
    context.fillStyle = gray(SUPPORT);
    context.fillRect(
      HUD_VISUAL_GEOMETRY.confirmationProgressX,
      246,
      Math.round(HUD_VISUAL_GEOMETRY.confirmationProgressWidth * progress),
      3,
    );
    context.fillStyle = gray(CHROME);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText(`${frame.secondsRemaining}s remaining`, 288, 267);
  }

  private drawIcon(
    context: CanvasRenderingContext2D,
    icon: HTMLImageElement,
    x: number,
    y: number,
    size: number,
    level: number,
  ): void {
    context.save();
    context.globalAlpha *= level / 255;
    context.drawImage(icon, x, y, size, size);
    context.restore();
  }

  private fitText(
    context: CanvasRenderingContext2D,
    text: string,
    x: number,
    y: number,
    maxWidth: number,
  ): void {
    let fitted = text;
    while (fitted.length > 1 && context.measureText(fitted).width > maxWidth) {
      fitted = fitted.slice(0, -1);
    }
    if (fitted !== text) fitted = `${fitted.slice(0, -1)}…`;
    context.fillText(fitted, x, y);
  }

  private drawWrappedText(
    context: CanvasRenderingContext2D,
    text: string,
    x: number,
    y: number,
    maxWidth: number,
    lineHeight: number,
    maximumLines: number,
  ): void {
    const lines = wrapHudText(text, maxWidth, (value) => context.measureText(value).width);
    const visible = visibleHudTailLines(lines, maximumLines);
    visible.forEach((value, index) => {
      this.fitText(context, value, x, y + index * lineHeight, maxWidth);
    });
  }

  private drawFittedWrappedText(
    context: CanvasRenderingContext2D,
    text: string,
    x: number,
    y: number,
    maxWidth: number,
    maximumLines: number,
  ): void {
    const previous = context.textAlign;
    context.textAlign = 'center';
    let lines: string[] = [];
    let fontSize = 21;
    for (; fontSize >= 12; fontSize -= 1) {
      context.font = `${fontSize}px ${HUD_FONT}`;
      lines = wrapHudText(text, maxWidth, (value) => context.measureText(value).width);
      if (lines.length <= maximumLines) break;
    }
    const lineHeight = Math.max(16, fontSize + 2);
    const firstY = y - ((lines.length - 1) * lineHeight) / 2;
    lines.forEach((line, index) => context.fillText(line, x, firstY + index * lineHeight));
    context.textAlign = previous;
  }
}
