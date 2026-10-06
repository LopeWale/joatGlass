import { GesturePolicy } from './GesturePolicy';
import {
  CreateStartUpPageContainer,
  RebuildPageContainer,
  TextContainerProperty,
  ImageContainerProperty,
  ListContainerProperty,
  ListItemContainerProperty,
  MenuContainerProperty,
  MenuItemProperty,
} from '@evenrealities/even_hub_sdk';
import { pxTruncate } from '@evenrealities/pretext';
import { TEXT_CONTAINER_MAX_BYTES, fitUtf8 } from './FirmwareLimits';
import { FIRMWARE_LINE_HEIGHT, containerLineCapacity, fitLines } from './FirmwareText';
import {
  CANVAS,
  CAPABILITIES,
  CAPABILITY_MENU_ID_BASE,
  ContainerId,
  VisualContainerName,
} from './types';
import {
  NATIVE_TERMINAL_GEOMETRY,
  NativeTerminalContainer,
  type NativeTerminalPage,
} from './NativeTerminalSurface';

/**
 * The firmware renders text at a fixed 27px line height and list rows at a
 * fixed 40px (font-measurement skill). Every container below is sized so its
 * INNER height (height − 2×padding) is a whole number of those units — the
 * 2026-09-05 audit found the previous 32px strips could not render a single
 * line, and the confirmation body clipped its own 120-char maximum.
 */
const FIRMWARE_LINE = FIRMWARE_LINE_HEIGHT;
const LIST_ROW = 40;
const REGION_PAD = 4;
/** One firmware line inside the region padding: the header of every
 * firmware page (status container) and where the page below it starts. */
export const NATIVE_HEADER_HEIGHT = FIRMWARE_LINE + 2 * REGION_PAD; // 35
/** Whole 40px rows between the header and the hint line. */
export const NATIVE_LIST_ROWS = 5;

/**
 * Fit the header to its line by shortening the page's WORD, never its tail:
 * a plain pixel cut would take the microphone and battery glyphs off the
 * glass first, and those are the facts the line exists to show. The word is
 * the second " · " segment ("9:41 · MESSAGES · CONVERSATION · ● · ▇" has
 * the title's own separator inside it, so the cut walks the whole middle).
 */
export function fitHeader(line: string, width = REGION_TEXT_WIDTH): string {
  const compact = line.replace(/\s+/g, ' ').trim();
  if (pxTruncate(compact, width) === compact) return compact;
  const parts = compact.split(' · ');
  if (parts.length < 3) return pxTruncate(compact, width);
  const head = parts[0]!;
  // Everything between the clock and the trailing glyphs is the word.
  const tailCount = parts.slice(1).filter((part) => /^[●○]$|^[▁▂▃▄▅▆▇█]\+?( \d{1,3}%)?$/.test(part)).length;
  const tail = tailCount > 0 ? parts.slice(parts.length - tailCount) : [];
  let word = parts.slice(1, parts.length - tail.length).join(' · ');
  while (word.length > 1) {
    word = `${word.slice(0, -2).trimEnd()}…`;
    const candidate = [head, word, ...tail].join(' · ');
    if (pxTruncate(candidate, width) === candidate) return candidate;
    if (word.length <= 2) break;
    word = word.slice(0, -1);
  }
  return pxTruncate([head, ...tail].join(' · '), width);
}
/** Inner text width of a full-width region (576 − 2×REGION_PAD). */
const REGION_TEXT_WIDTH = CANVAS.width - 2 * REGION_PAD;
/** A list row's text width: container pad (8) + firmware row pad (12/side). */
const LIST_ROW_TEXT_WIDTH = CANVAS.width - 2 * 8 - 2 * 12;
/**
 * The host's real per-row limit, measured in the official simulator on
 * 2026-10-05: a row of 64 UTF-8 bytes or more makes `rebuildPageContainer`
 * return false for the WHOLE page (a 64-byte buffer with its terminator), and
 * the SDK validates nothing. The skill's "64 chars" is 63 bytes, and a
 * middle dot is two of them. A Messages thread row at the Mac's 64-unit cap
 * reproduced it: the thread never appeared and the menu fell back Home.
 */
export const LIST_ROW_MAX_UTF8_BYTES = 63;

/** The raster prompt's footer, kept word for word on the text page. */
export const PROMPT_QUESTION_HINT = 'speak your answer · double-click: cancel';

/** Cut on a code-point boundary so the row stays under the host's byte cap. */
export function clampUtf8Bytes(text: string, maxBytes: number): string {
  let bytes = 0;
  let out = '';
  for (const point of text) {
    const size = new TextEncoder().encode(point).length;
    if (bytes + size > maxBytes) break;
    bytes += size;
    out += point;
  }
  return out;
}

/** Native conversation geometry. The card is anchored at the same optical
 * bottom while its top moves upward through bounded line-count stages. */
/** The firmware answer page: one status line, one tall body, one hint
 * line. The body holds seven firmware lines; longer answers page. */
/** Firmware lines the confirmation page's body holds (its main is 144 px). */
export const CONFIRMATION_BODY_LINES = 5;

export const ASSISTANT_ANSWER_PAGE = {
  bodyY: NATIVE_HEADER_HEIGHT,
  bodyHeight: 253 - NATIVE_HEADER_HEIGHT, // 218: down to the hint line
  lineCapacity: containerLineCapacity(253 - NATIVE_HEADER_HEIGHT, REGION_PAD), // 7, measured as the firmware wraps
  textWidth: REGION_TEXT_WIDTH,
} as const;

export const ASSISTANT_CONVERSATION_GEOMETRY = {
  cardLeft: 36,
  cardWidth: 504,
  /** The card's lower edge. The live header line sits above the card, so
   * the card sits 14 px off the bottom (the lens margin HUD_EDGE keeps)
   * rather than 28: seven transcript lines still fit under the header. */
  cardBottom: 274,
  transcriptLeft: 52,
  transcriptWidth: 458,
  footerLeft: 52,
  footerWidth: 458,
  scrollbarLeft: 516,
  scrollbarWidth: 18,
  lineHeight: 27,
  cardTopPadding: 8,
  footerGap: 5,
  footerBottomInset: 4,
} as const;

/** Page-local ids. They intentionally do not enter the Mac wire protocol;
 * only the Hub owns and updates these native containers. */
export const AssistantConversationContainerId = {
  Frame: 11,
  WearerTurns: 12,
  JoatTurns: 13,
  ScrollPosition: 14,
  Generation: 15,
} as const;

export interface AssistantConversationPage {
  cardTop: number;
  cardHeight: number;
  transcriptTop: number;
  transcriptHeight: number;
  dimText: string;
  brightText: string;
  scrollbarText: string;
  footerText: string;
}


/**
 * Declares HUD layouts against the 576x288 canvas.
 *
 * Invariants enforced here so they can never drift:
 *  - exactly one container carries isEventCapture: 1
 *  - zOrderIndex is set on every container and is unique (all-or-nothing rule)
 *  - containerTotalNum matches the number of declared containers
 */
export class HudPageBuilder {
  constructor(private readonly contextualMenu = true) {}

  /** Freeze the host capability negotiated at boot into every later layout. */
  withContextualMenu(enabled: boolean): HudPageBuilder {
    return new HudPageBuilder(enabled);
  }

  /** The one-line header of every firmware page: clock · page · mic ·
   * battery (HudStatus.nativeStatusLine), composed by the presenter. It
   * replaced a status line in words plus a separate title line, so each
   * page below it starts 42 px higher than it did — a fifth list row. */
  private statusBar(content = '', zOrderIndex = 1): TextContainerProperty {
    return new TextContainerProperty({
      containerID: ContainerId.StatusBar,
      containerName: 'status',
      xPosition: 0,
      yPosition: 0,
      width: CANVAS.width,
      height: NATIVE_HEADER_HEIGHT, // 35: one firmware line inside 4px padding
      paddingLength: REGION_PAD,
      borderWidth: 0,
      isEventCapture: 0,
      zOrderIndex,
      textColor: 2,
      content: fitHeader(content),
    });
  }

  /**
   * `isEventCapture` is a PARAMETER, not a constant: only ONE container per
   * page may capture, and in the list layout that container must be the list
   * — the firmware moves the selection highlight and emits `listEvent` only
   * for the capturing list. Leaving capture here draws the rows but makes
   * them unnavigable (swipes come back as `textEvent`, presses as `sysEvent`).
   */
  private main(
    height: number,
    isEventCapture: 0 | 1 = 1,
    textColor = 4,
    content = '',
    geometry: { x?: number; y?: number; width?: number } = {},
  ): TextContainerProperty {
    return new TextContainerProperty({
      containerID: ContainerId.Main,
      containerName: 'main',
      xPosition: geometry.x ?? 0,
      yPosition: geometry.y ?? NATIVE_HEADER_HEIGHT,
      width: geometry.width ?? CANVAS.width,
      height,
      paddingLength: REGION_PAD,
      borderWidth: 0,
      isEventCapture,
      zOrderIndex: 2,
      textColor,
      content: fitUtf8(content, TEXT_CONTAINER_MAX_BYTES),
    });
  }

  /**
   * A dashboard region: one owner, one purpose (docs/HUD_DASHBOARD.md).
   * `textColor` is firmware brightness 0-4 — chrome dim, content bright, so
   * hierarchy survives on transparent glass where color cannot carry it.
   */
  private region(
    id: ContainerId, name: string, y: number, height: number,
    z: number, textColor: number, content = '',
    geometry: { x?: number; width?: number; isEventCapture?: 0 | 1 } = {},
  ): TextContainerProperty {
    return new TextContainerProperty({
      containerID: id,
      containerName: name,
      xPosition: geometry.x ?? 0,
      yPosition: y,
      width: geometry.width ?? CANVAS.width,
      height,
      paddingLength: REGION_PAD,
      borderWidth: 0,
      isEventCapture: geometry.isEventCapture ?? 0,
      zOrderIndex: z,
      textColor,
      content: fitUtf8(content, TEXT_CONTAINER_MAX_BYTES),
    });
  }

  /**
   * The dashboard: five regions, each with a single owner, so content stops
   * colliding. Before this everything shared MAIN — a reply erased the
   * wearer's own words 200ms after showing them, and a notification card
   * erased the reply. Regions also let the PLUGIN paint chrome (status,
   * hints) with no Mac attached, instead of a frozen screen.
   *
   *   0   status  35   plugin: clock, mic, battery   (1 line)
   *   35  main   121   Mac: reply / card / debate  <-- capture (4 lines)
   *   156 context 62   Mac: what the wearer last said        (2 lines)
   *   218 tray    35   Mac: what is waiting                  (1 line)
   *   253 hint    35   plugin: gesture hints, blank at rest  (1 line)
   */
  private dashboard(): TextContainerProperty[] {
    return [
      this.statusBar(),
      this.main(156 - NATIVE_HEADER_HEIGHT),
      this.region(ContainerId.Context, 'context', 156, 62, 3, 3),
      this.region(ContainerId.Tray, 'tray', 218, 35, 4, 2),
      this.region(ContainerId.Hint, 'hint', 253, 35, 5, 1),
    ];
  }

  /**
   * The glasses-OS contextual menu (long-press): stable ids the wearer can
   * reach from any negotiated menu-capable screen without costing the
   * display. The builder's boot-time capability flag is applied to every
   * later layout so a host that rejected menuObject is never sent it again.
   */
  static readonly MENU_REPEAT = 1;
  static readonly MENU_MUTE = 2;
  /** Retired row id, still answered for a host showing a cached menu: it
   * opens the Settings guidance (connection settings live on the phone). */
  static readonly MENU_REFRESH = 3;
  static readonly MENU_SETTINGS = 4;
  /** A non-destructive escape from any visual or text focus surface. */
  static readonly MENU_PLANNING = 5;

  /**
   * THE menu — the same capability rows the plugin router opens, offered
   * through the mechanism the platform actually provides for one.
   *
   * `menuObject` is the firmware's contextual menu (SDK: "top-level
   * contextual-menu entries; firmware accepts at most 10"). It is drawn by
   * the glasses, so it costs ZERO image tiles, where our own menu page costs
   * two — and an image frame is 0.5-2s over BLE. Registering leftovers here
   * while the real menu lived in our tiles is why the menu gesture opened a
   * menu the wearer did not recognise.
   *
   * Mute keeps its long-press gesture; connection and listening settings live
   * on the phone. The retired Settings slot has no visible menu entry.
   * The retired ids stay handled in EventRouter because
   * a host may still be showing a cached menu.
   */
  private systemMenu(): MenuContainerProperty {
    return new MenuContainerProperty({
      // Filter after attaching the original index so surviving action IDs
      // never move when a phone-owned entry disappears from the menu.
      menuItems: CAPABILITIES.map((name, index) => ({ name, index }))
        .filter(({ name }) => name !== 'Settings')
        .map(({ name, index }) => new MenuItemProperty({
        itemID: CAPABILITY_MENU_ID_BASE + index,
        itemName: name,
      })),
    });
  }

  /**
   * The overview as firmware text. No image tiles.
   *
   * GEOMETRY IS DERIVED, NOT INVENTED. The firmware line box is 27px
   * (LINE_HEIGHT), the canvas is 576x288, and Even's multi-row pattern is
   * stacked text containers with explicit heights (everything-evenhub
   * skills/glasses-ui, "Multi-row layout"). Every band below is a whole
   * number of 27px lines plus its 4px padding, and the bands are laid out
   * head to tail so none can overlap:
   *
   * A band's height is lines*27 PLUS its padding top and bottom. Sizing a
   * band short of that overflows it, and the firmware draws a scrollbar
   * down its right edge. The current table is in the body below.
   *
   * THE TAB RAIL IS THE CAPTURE CONTAINER, NOT THE PANE. A capturing text
   * container scrolls its own overflow and reports a swipe only at its
   * ends (even-g2-notes), so with the pane capturing, a News card longer
   * than six lines swallowed every swipe: the rail never moved and the
   * next tap expanded the wrong tab (observed 2026-09-22). The rail never
   * overflows, so every swipe reaches the plugin and steps the rail. The
   * pane is a PREVIEW — seven rows, each cut to one line — and the expanded
   * tab is where the whole list lives.
   */
  overviewTextLayout(view: {
    status: string; clock: string; date: string; ambient: string;
    tabs: readonly { label: string; active: boolean }[];
    paneTitle: string; paneBody: string; hint: string;
    /** Mac urgency carries into the pane, same as the canvas overview. */
    paneBrightness?: number;
    /** A fourth fact for the clock column, e.g. "3 unread". */
    extra?: string;
  }): RebuildPageContainer {
    // FULL-CANVAS GEOMETRY, measured with @evenrealities/pretext (27px
    // firmware line, inner box = size − 2×padding). The 2026-10-05 audit of
    // the previous cut found it left 7px unused at the bottom and 2px gaps
    // between every band, showed six pane rows where seven fit, printed the
    // time twice (status AND clock), and gave the 8px divider 4px padding —
    // an inner width of ZERO, so its seven dots wrapped to fourteen lines
    // and the firmware drew a scrollbar where the divider should be.
    //
    // Now ten firmware lines tile the whole 576x288 canvas, 2px inset:
    //
    //   y=0    h=31   status            1 line, full width
    //   y=31   h=112  clock column      4 lines | pane title  1 line
    //   y=143  h=112  tab rail          4 lines | pane        7 lines -> 255
    //   y=257  h=31   hint              1 line, full width          -> 288
    //
    // Left column 150px: inner 146 holds "> NOTIFICATIONS" (144px).
    // Divider: a 2px bordered rule centred in the 8px gutter at x=153.
    // Right column 158..576: inner 414px per pane row.
    const PAD = 2;
    const LINE = 27;
    const LINE_BAND = (lines: number): number => lines * LINE + 2 * PAD;
    const LEFT_W = 150;
    const DIVIDER_W = 8;   // gutter; the rule itself is 2px at its centre
    const RIGHT_X = LEFT_W + DIVIDER_W;
    const RIGHT_W = CANVAS.width - RIGHT_X;
    const TOP = LINE_BAND(1);                       // 31
    const PANE_LINES = 7;
    const HINT_Y = CANVAS.height - LINE_BAND(1);    // 257
    // Trailing whitespace only: a leading run is the rail's indent.
    const text = (value: string): string => (value.trim() ? value.replace(/\s+$/, '') : ' ');
    // Every band, not just the pane, is cut to what its inner box holds
    // (font-measurement skill: measure against width − 2×(padding+border),
    // pxTruncate per line). Status, title, hint and the clock column carry
    // Mac-supplied text; uncut, a long title or hint wrapped to a second
    // line inside a one-line band and the firmware drew a scrollbar.
    const fit = (value: string, width: number, height: number, padding: number): string => {
      const innerW = width - 2 * padding;
      const lines = Math.max(1, Math.floor((height - 2 * padding) / LINE));
      return text(value).split('\n').slice(0, lines)
        .map((line) => pxTruncate(line, innerW)).join('\n');
    };
    const preview = view.paneBody.split('\n')
      .map((row) => row.trim()).filter(Boolean)
      .slice(0, PANE_LINES)
      .map((row) => pxTruncate(row, RIGHT_W - 2 * PAD))
      .join('\n');
    // '>' as the cursor, which is Even's documented convention for marking
    // a selected row ("Prefix text items with > as a cursor indicator").
    // The font is proportional: '> ' measures 15px and a space 5px, so an
    // unselected row is indented THREE spaces to keep every label on the
    // same x (two spaces left them 5px left of the selected one).
    const rail = view.tabs
      .map(({ label, active }) => `${active ? '> ' : '   '}${label}`)
      .join('\n');
    const band = (
      containerID: number, containerName: string,
      x: number, y: number, width: number, height: number,
      textColor: number, content: string, capture = 0, z = 1, padding = PAD,
    ): TextContainerProperty => new TextContainerProperty({
      containerID, containerName,
      xPosition: x, yPosition: y, width, height,
      paddingLength: padding, borderWidth: 0,
      isEventCapture: capture, zOrderIndex: z,
      textColor, content: fitUtf8(fit(content, width, height, padding), TEXT_CONTAINER_MAX_BYTES),
    });
    // The time lives here only; the status line carries the date.
    const clockColumn = [view.clock, view.ambient, view.extra ?? '']
      .filter((l) => l.trim()).slice(0, 4).join('\n');
    return new RebuildPageContainer({
      containerTotalNum: 7,
      ...this.menuConfig(),
      textObject: [
        band(ContainerId.StatusBar, 'status', 0, 0, CANVAS.width, TOP, 2, view.status, 0, 1),
        band(ContainerId.Context, 'clock', 0, TOP, LEFT_W, LINE_BAND(4), 4, clockColumn, 0, 2),
        band(ContainerId.Tray, 'tabs', 0, TOP + LINE_BAND(4), LEFT_W, LINE_BAND(4), 2, rail, 1, 3),
        band(ContainerId.List, 'pane-title', RIGHT_X, TOP, RIGHT_W, LINE_BAND(1), 2, view.paneTitle, 0, 4),
        band(ContainerId.Main, 'pane', RIGHT_X, TOP + LINE_BAND(1), RIGHT_W, LINE_BAND(PANE_LINES),
          view.paneBrightness ?? 4, preview, 0, 5),
        // The column divider: a 2px box whose 1px border draws one solid
        // vertical rule the full height of the middle band. The previous
        // "line" was an accident — the dotted divider overflowed its
        // zero-width box and the firmware drew a SCROLLBAR there, which
        // stopped two-thirds of the way down. Static: never upgraded.
        new TextContainerProperty({
          containerID: ContainerId.Image, containerName: 'divider',
          xPosition: LEFT_W + 3, yPosition: TOP + PAD, width: 2, height: 8 * LINE,
          paddingLength: 0, borderWidth: 1, borderColor: 6, borderRadius: 0,
          isEventCapture: 0, zOrderIndex: 7, textColor: 1, content: ' ',
        }),
        band(ContainerId.Hint, 'hint', 0, HINT_Y, CANVAS.width, LINE_BAND(1), 2,
          GesturePolicy.formatHint(view.hint), 0, 6),
      ],
    });
  }

  private menuConfig(): { menuObject?: MenuContainerProperty } {
    return this.contextualMenu ? { menuObject: this.systemMenu() } : {};
  }

  private minimalStartupText(): TextContainerProperty {
    return new TextContainerProperty({
      containerID: ContainerId.Main,
      containerName: 'startup',
      xPosition: 0,
      yPosition: 0,
      width: CANVAS.width,
      height: CANVAS.height,
      paddingLength: 9, // inner 270 = ten firmware lines exactly
      borderWidth: 0,
      isEventCapture: 1,
      zOrderIndex: 1,
      textColor: 4,
      content: 'JOAT\nStarting...',
    });
  }

  /** The known-visible fallback page after a host rejects the visual startup. */
  compatibilityLayout(): RebuildPageContainer {
    return new RebuildPageContainer({
      containerTotalNum: 1,
      textObject: [this.minimalStartupText()],
    });
  }

  /** Full text HUD without the optional OS contextual menu. */
  dashboardCompatibilityLayout(): RebuildPageContainer {
    return new RebuildPageContainer({
      containerTotalNum: 5,
      textObject: this.dashboard(),
    });
  }

  /** The same five regions, for rebuildPageContainer. */
  defaultLayout(): RebuildPageContainer {
    return new RebuildPageContainer({
      containerTotalNum: 5,
      textObject: this.dashboard(),
      ...this.menuConfig(),
    });
  }

  /**
   * Pixel-designed HUD: four maximum-size image tiles form one 576x288 frame.
   * Images cannot capture events, so one hardware-valid space-only text
   * container spans the canvas behind them and preserves input delivery.
   */
  private visualObjects(): {
    textObject: TextContainerProperty[];
    imageObject: ImageContainerProperty[];
  } {
    return {
      textObject: [new TextContainerProperty({
        containerID: ContainerId.Main,
        containerName: 'visual-input',
        xPosition: 0,
        yPosition: 0,
        width: CANVAS.width,
        height: CANVAS.height,
        paddingLength: 0,
        borderWidth: 0,
        isEventCapture: 1,
        zOrderIndex: 1,
        textColor: 0,
        content: ' ',
      })],
      imageObject: [
        new ImageContainerProperty({
          containerID: ContainerId.Image,
          containerName: VisualContainerName[ContainerId.Image],
          xPosition: 0, yPosition: 0, width: 288, height: 144, zOrderIndex: 2,
        }),
        new ImageContainerProperty({
          containerID: ContainerId.VisualTopRight,
          containerName: VisualContainerName[ContainerId.VisualTopRight],
          xPosition: 288, yPosition: 0, width: 288, height: 144, zOrderIndex: 3,
        }),
        new ImageContainerProperty({
          containerID: ContainerId.VisualBottomLeft,
          containerName: VisualContainerName[ContainerId.VisualBottomLeft],
          xPosition: 0, yPosition: 144, width: 288, height: 144, zOrderIndex: 4,
        }),
        new ImageContainerProperty({
          containerID: ContainerId.VisualBottomRight,
          containerName: VisualContainerName[ContainerId.VisualBottomRight],
          xPosition: 288, yPosition: 144, width: 288, height: 144, zOrderIndex: 5,
        }),
      ],
    };
  }

  visualStartup(): CreateStartUpPageContainer {
    return new CreateStartUpPageContainer({
      containerTotalNum: 5,
      ...this.visualObjects(),
      ...this.menuConfig(),
    });
  }

  visualLayout(): RebuildPageContainer {
    return new RebuildPageContainer({
      containerTotalNum: 5,
      ...this.visualObjects(),
      // Rebuild replaces a contextual menu wholesale when it is omitted.
      // Keep the same declared actions on the production image HUD as on the
      // text fallback, so a visual transition never removes the wearer's
      // non-destructive escape route.
      ...this.menuConfig(),
    });
  }

  /**
   * One Even-style native conversation card. Wearer and JOAT text occupy
   * aligned transparent layers so the fixed firmware font can express role
   * hierarchy with brightness even though it exposes no bold font weight.
   * The border is the single event owner; the plugin moves both text layers
   * together on scroll instead of asking one firmware container to scroll
   * independently of the other.
   */
  assistantConversationLayout(view: AssistantConversationPage, status = ''): RebuildPageContainer {
    const geometry = ASSISTANT_CONVERSATION_GEOMETRY;
    const content = (value: string): string => value || ' ';
    return new RebuildPageContainer({
      containerTotalNum: 6,
      ...this.menuConfig(),
      textObject: [
        // The live header line every page carries: clock, the other running
        // features (Pomodoro, next turn), mic and battery. A conversation
        // used to have none, so a timer or a route vanished behind it.
        this.statusBar(status, 6),
        new TextContainerProperty({
          containerID: AssistantConversationContainerId.Frame,
          containerName: 'conversation',
          xPosition: geometry.cardLeft,
          yPosition: view.cardTop,
          width: geometry.cardWidth,
          height: view.cardHeight,
          paddingLength: 0,
          borderWidth: 1,
          borderColor: 12,
          borderRadius: 4,
          isEventCapture: 1,
          zOrderIndex: 1,
          textColor: 0,
          content: ' ',
        }),
        new TextContainerProperty({
          containerID: AssistantConversationContainerId.WearerTurns,
          containerName: 'wearer-turns',
          xPosition: geometry.transcriptLeft,
          yPosition: view.transcriptTop,
          width: geometry.transcriptWidth,
          height: view.transcriptHeight,
          paddingLength: 0,
          borderWidth: 0,
          isEventCapture: 0,
          zOrderIndex: 2,
          textColor: 2,
          content: fitUtf8(content(view.dimText), TEXT_CONTAINER_MAX_BYTES),
        }),
        new TextContainerProperty({
          containerID: AssistantConversationContainerId.JoatTurns,
          containerName: 'joat-turns',
          xPosition: geometry.transcriptLeft,
          yPosition: view.transcriptTop,
          width: geometry.transcriptWidth,
          height: view.transcriptHeight,
          paddingLength: 0,
          borderWidth: 0,
          isEventCapture: 0,
          zOrderIndex: 3,
          textColor: 4,
          content: fitUtf8(content(view.brightText), TEXT_CONTAINER_MAX_BYTES),
        }),
        new TextContainerProperty({
          containerID: AssistantConversationContainerId.ScrollPosition,
          containerName: 'scroll-position',
          xPosition: geometry.scrollbarLeft,
          yPosition: view.transcriptTop,
          width: geometry.scrollbarWidth,
          height: view.transcriptHeight,
          paddingLength: 0,
          borderWidth: 0,
          isEventCapture: 0,
          zOrderIndex: 4,
          textColor: 2,
          content: content(view.scrollbarText),
        }),
        new TextContainerProperty({
          containerID: AssistantConversationContainerId.Generation,
          containerName: 'generation',
          xPosition: geometry.footerLeft,
          yPosition: geometry.cardBottom - geometry.footerBottomInset - geometry.lineHeight,
          width: geometry.footerWidth,
          height: geometry.lineHeight,
          paddingLength: 0,
          borderWidth: 0,
          isEventCapture: 0,
          zOrderIndex: 5,
          textColor: 4,
          content: content(GesturePolicy.formatHint(view.footerText)),
        }),
      ],
    });
  }

  /** One fixed firmware-text document for a selected terminal provider.
   * The complete dynamic body is replaced in a single SDK transaction, so a
   * provider revision or page gesture cannot expose mixed quadrants or mixed
   * old/new terminal regions. */
  nativeTerminalLayout(page: NativeTerminalPage): RebuildPageContainer {
    const geometry = NATIVE_TERMINAL_GEOMETRY;
    return new RebuildPageContainer({
      containerTotalNum: 2,
      ...this.menuConfig(),
      textObject: [
        new TextContainerProperty({
          containerID: NativeTerminalContainer.Head.id,
          containerName: NativeTerminalContainer.Head.name,
          xPosition: 0,
          yPosition: geometry.headY,
          width: geometry.width,
          height: geometry.headHeight,
          paddingLength: geometry.padding,
          borderWidth: 0,
          isEventCapture: 0,
          zOrderIndex: 1,
          textColor: 2,
          content: fitUtf8(page.headText || ' ', TEXT_CONTAINER_MAX_BYTES),
        }),
        new TextContainerProperty({
          containerID: NativeTerminalContainer.Body.id,
          containerName: NativeTerminalContainer.Body.name,
          xPosition: 0,
          yPosition: geometry.bodyY,
          width: geometry.width,
          height: geometry.bodyHeight,
          paddingLength: geometry.padding,
          borderWidth: 0,
          isEventCapture: 1,
          zOrderIndex: 2,
          textColor: 4,
          content: fitUtf8(page.bodyText || ' ', TEXT_CONTAINER_MAX_BYTES),
        }),
      ],
    });
  }

  /**
   * The full-width firmware list page: one header line (clock · page name ·
   * mic · battery), FIVE visible 40px rows (the firmware scrolls the rest),
   * one-line hint. Until 2026-10-05 the page spent two lines up top — a
   * status line in words and a separate title line — and showed four rows;
   * folding the name into the header is where the fifth row comes from.
   * Text is truncated by MEASURED pixels (@evenrealities/pretext), not
   * characters — the audit showed a 64-char row spans 705-1024px against a
   * 536px row.
   */
  listLayout(
    items: string[],
    chrome: { title?: string; hint?: string; status?: string } = {},
  ): RebuildPageContainer {
    const capped = items.slice(0, 20)
      .map((s) => pxTruncate(clampUtf8Bytes(s, LIST_ROW_MAX_UTF8_BYTES), LIST_ROW_TEXT_WIDTH));
    // The presenter composes the header with the page's name in it; a bare
    // title here is the no-status fallback, so the page is never nameless.
    const header = (chrome.status?.trim() || chrome.title?.trim() || 'JOAT').slice(0, 64);
    // 96 mirrors HudPresenter.MAX_FOCUS_HINT_CHARS — the pixel truncation
    // below is the real bound; a tighter char pre-cut here silently dropped
    // half the presenter's copy (2026-09-05 audit).
    const hint = pxTruncate(GesturePolicy.formatHint(chrome.hint?.trim() || 'double-click: overview').slice(0, 96), REGION_TEXT_WIDTH);
    return new RebuildPageContainer({
      containerTotalNum: 3,
      ...this.menuConfig(),
      // Capture is ON the list — see main()'s note.
      textObject: [
        this.statusBar(header),
        this.region(ContainerId.Hint, 'hint', 253, 35, 4, 1, hint),
      ],
      listObject: [
        new ListContainerProperty({
          containerID: ContainerId.List,
          containerName: 'list',
          xPosition: 0,
          yPosition: NATIVE_HEADER_HEIGHT,
          width: CANVAS.width,
          height: NATIVE_LIST_ROWS * LIST_ROW + 16, // 216: five whole rows inside 8px padding
          paddingLength: 8,
          // The capturing container for this layout: firmware scrolls the
          // highlight on swipe and reports the pick as `listEvent`.
          isEventCapture: 1,
          zOrderIndex: 3,
          itemContainer: new ListItemContainerProperty({
            itemCount: capped.length,
            itemWidth: 0, // auto — fill container
            isItemSelectBorderEn: 1,
            itemName: capped,
          }),
        }),
      ],
    });
  }

  /** A JOAT answer as Even's own text page: a command result, an error, a
   * capability answer. One rebuild in, in-place upgrades for paging and
   * streaming, and the four image containers return on dismissal. */
  assistantAnswerLayout(
    body: string,
    chrome: { status?: string; hint?: string } = {},
  ): RebuildPageContainer {
    return new RebuildPageContainer({
      containerTotalNum: 3,
      ...this.menuConfig(),
      textObject: [
        this.statusBar(chrome.status ?? ''),
        this.main(ASSISTANT_ANSWER_PAGE.bodyHeight, 1, 4, (body || ' ').slice(0, 1000)),
        this.region(
          ContainerId.Hint, 'hint', 253, 35, 3, 1,
          pxTruncate((chrome.hint ?? 'tap: dismiss').slice(0, 96), REGION_TEXT_WIDTH),
        ),
      ],
    });
  }

  /** A Mac-paged reader (a Messages message, a note) as Even's own text
   * page: the same three containers as an answer, so the presenter can page
   * it in place with `textContainerUpgrade`. */
  textPageLayout(
    body: string,
    chrome: { status?: string; hint?: string } = {},
  ): RebuildPageContainer {
    return this.assistantAnswerLayout(body, chrome);
  }

  /** One complete application question ("What should I say?") as a text
   * page. The wearer answers by voice, so the only chrome is the cancel. */
  promptQuestionLayout(question: string, status?: string): RebuildPageContainer {
    return this.assistantAnswerLayout(question, {
      status, hint: PROMPT_QUESTION_HINT,
    });
  }

  /** Dedicated decision surface: one bright question, one horizontal CTA row.
   * The body holds CONFIRMATION_BODY_LINES firmware lines: "CONFIRM ACTION",
   * a blank, then the prompt on the three that remain, fitted by
   * `measureTextWrap` (whole words, an ellipsis when cut) rather than by a
   * character count — a prompt of wide glyphs never overflows, one of
   * narrow glyphs is not cut short. */
  confirmationLayout(prompt: string, status?: string): RebuildPageContainer {
    const question = fitLines(
      prompt.trim() || 'Confirm requested action.', REGION_TEXT_WIDTH, CONFIRMATION_BODY_LINES - 2,
    );
    return new RebuildPageContainer({
      containerTotalNum: 5,
      ...this.menuConfig(),
      textObject: [
        // Over the visual HUD this page is firmware-owned and the presenter
        // cannot upgrade its containers, so the live top line rides along.
        this.statusBar(status ?? ''),
        this.main(CONFIRMATION_BODY_LINES * FIRMWARE_LINE + 2 * REGION_PAD + 1, 1, 4, `CONFIRM ACTION\n\n${question}`), // 144
        this.region(
          ContainerId.Context, 'context', 183, 35, 3, 3,
          'TAP · confirm   SWIPE · cancel',
        ),
        this.region(ContainerId.Tray, 'tray', 218, 35, 4, 3, ''),
        this.region(ContainerId.Hint, 'hint', 253, 35, 5, 1, '8 seconds · timeout cancels'),
      ],
    });
  }
}
