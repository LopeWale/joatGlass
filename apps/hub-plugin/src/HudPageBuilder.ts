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
import {
  CANVAS,
  CAPABILITIES,
  CAPABILITY_MENU_ID_BASE,
  ContainerId,
  VisualContainerName,
} from './types';

/**
 * The firmware renders text at a fixed 27px line height and list rows at a
 * fixed 40px (font-measurement skill). Every container below is sized so its
 * INNER height (height − 2×padding) is a whole number of those units — the
 * 2026-09-05 audit found the previous 32px strips could not render a single
 * line, and the confirmation body clipped its own 120-char maximum.
 */
const FIRMWARE_LINE = 27;
const LIST_ROW = 40;
const REGION_PAD = 4;
/** Inner text width of a full-width region (576 − 2×REGION_PAD). */
const REGION_TEXT_WIDTH = CANVAS.width - 2 * REGION_PAD;
/** A list row's text width: container pad (8) + firmware row pad (12/side). */
const LIST_ROW_TEXT_WIDTH = CANVAS.width - 2 * 8 - 2 * 12;

/** Native conversation geometry. The card is anchored at the same optical
 * bottom while its top moves upward through bounded line-count stages. */
export const ASSISTANT_CONVERSATION_GEOMETRY = {
  cardLeft: 36,
  cardWidth: 504,
  cardBottom: 260,
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

  private statusBar(): TextContainerProperty {
    return new TextContainerProperty({
      containerID: ContainerId.StatusBar,
      containerName: 'status',
      xPosition: 0,
      yPosition: 0,
      width: CANVAS.width,
      height: FIRMWARE_LINE + 12, // 39: one firmware line inside 6px padding
      paddingLength: 6,
      borderWidth: 0,
      isEventCapture: 0,
      zOrderIndex: 1,
      textColor: 2,
      content: '',
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
      yPosition: geometry.y ?? 39,
      width: geometry.width ?? CANVAS.width,
      height,
      paddingLength: REGION_PAD,
      borderWidth: 0,
      isEventCapture,
      zOrderIndex: 2,
      textColor,
      content,
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
      content,
    });
  }

  /**
   * The dashboard: five regions, each with a single owner, so content stops
   * colliding. Before this everything shared MAIN — a reply erased the
   * wearer's own words 200ms after showing them, and a notification card
   * erased the reply. Regions also let the PLUGIN paint chrome (status,
   * hints) with no Mac attached, instead of a frozen screen.
   *
   *   0   status  39   plugin: clock, mic, battery   (1 line)
   *   39  main   117   Mac: reply / card / debate  <-- capture (4 lines)
   *   156 context 62   Mac: what the wearer last said        (2 lines)
   *   218 tray    35   Mac: what is waiting                  (1 line)
   *   253 hint    35   plugin: gesture hints, blank at rest  (1 line)
   */
  private dashboard(): TextContainerProperty[] {
    return [
      this.statusBar(),
      this.main(117),
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
  /** Universal recovery: tap, then long-press, then choose Refresh. */
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
   * Kept as a named alias for older focused fixtures. The transparent text
   * layer remains the sole gesture owner; the OS-owned contextual overlay is
   * independently declared on the page when the negotiated host supports it.
   */
  visualCompatibilityLayout(): RebuildPageContainer {
    return this.visualLayout();
  }

  /**
   * One Even-style native conversation card. Wearer and JOAT text occupy
   * aligned transparent layers so the fixed firmware font can express role
   * hierarchy with brightness even though it exposes no bold font weight.
   * The border is the single event owner; the plugin moves both text layers
   * together on scroll instead of asking one firmware container to scroll
   * independently of the other.
   */
  assistantConversationLayout(view: AssistantConversationPage): RebuildPageContainer {
    const geometry = ASSISTANT_CONVERSATION_GEOMETRY;
    const content = (value: string): string => value || ' ';
    return new RebuildPageContainer({
      containerTotalNum: 5,
      ...this.menuConfig(),
      textObject: [
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
          content: content(view.dimText),
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
          content: content(view.brightText),
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

  /**
   * The full-width firmware list page: status, one-line title, four visible
   * 40px rows (the firmware scrolls the rest), one-line hint. Text is
   * truncated by MEASURED pixels (@evenrealities/pretext), not characters —
   * the audit showed a 64-char row spans 705-1024px against a 536px row.
   */
  listLayout(
    items: string[],
    chrome: { title?: string; hint?: string } = {},
  ): RebuildPageContainer {
    const capped = items.slice(0, 20)
      .map((s) => pxTruncate(s.slice(0, 64), LIST_ROW_TEXT_WIDTH));
    const title = pxTruncate((chrome.title?.trim() || 'JOAT').slice(0, 46), REGION_TEXT_WIDTH);
    // 96 mirrors HudPresenter.MAX_FOCUS_HINT_CHARS — the pixel truncation
    // below is the real bound; a tighter char pre-cut here silently dropped
    // half the presenter's copy (2026-09-05 audit).
    const hint = pxTruncate(GesturePolicy.formatHint(chrome.hint?.trim() || 'double-click: overview').slice(0, 96), REGION_TEXT_WIDTH);
    return new RebuildPageContainer({
      containerTotalNum: 4,
      ...this.menuConfig(),
      // Capture moves OFF main and ONTO the list — see main()'s note.
      textObject: [
        this.statusBar(),
        this.main(FIRMWARE_LINE + 2 * REGION_PAD + 3, 0, 4, title), // 38: one line
        this.region(ContainerId.Hint, 'hint', 253, 35, 4, 1, hint),
      ],
      listObject: [
        new ListContainerProperty({
          containerID: ContainerId.List,
          containerName: 'list',
          xPosition: 0,
          yPosition: 77,
          width: CANVAS.width,
          height: 4 * LIST_ROW + 16, // 176: four whole rows inside 8px padding
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

  /** Dedicated decision surface: one bright question, one horizontal CTA row.
   * The body holds FIVE firmware lines because its own 120-char maximum
   * measures five ("CONFIRM ACTION" + blank + up to three wrapped lines). */
  confirmationLayout(prompt: string): RebuildPageContainer {
    const safePrompt = (prompt.trim() || 'Confirm requested action.').slice(0, 120);
    return new RebuildPageContainer({
      containerTotalNum: 5,
      ...this.menuConfig(),
      textObject: [
        this.statusBar(),
        this.main(5 * FIRMWARE_LINE + 2 * REGION_PAD + 1, 1, 4, `CONFIRM ACTION\n\n${safePrompt}`), // 144
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
