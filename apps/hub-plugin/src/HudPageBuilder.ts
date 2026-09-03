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
import {
  CANVAS,
  CAPABILITIES,
  CAPABILITY_MENU_ID_BASE,
  ContainerId,
  SPINE_LABEL_BOX,
  SPINE_LABEL_BRIGHTNESS,
  SPINE_LABEL_NAME,
  VisualContainerName,
} from './types';


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
      height: 32,
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
      yPosition: geometry.y ?? 32,
      width: geometry.width ?? CANVAS.width,
      height,
      paddingLength: 8,
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
      paddingLength: 8,
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
   *   0   status  32   plugin: clock, link, mic, battery
   *   32  main   128   Mac: reply / card / debate  <-- capture
   *   160 context 64   Mac: what the wearer last said
   *   224 tray    32   Mac: what is waiting
   *   256 hint    32   plugin: gesture hints, blank at rest
   */
  private dashboard(): TextContainerProperty[] {
    return [
      this.statusBar(),
      this.main(128),
      this.region(ContainerId.Context, 'context', 160, 64, 3, 3),
      this.region(ContainerId.Tray, 'tray', 224, 32, 4, 2),
      this.region(ContainerId.Hint, 'hint', 256, 32, 5, 1),
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
   * Mute keeps its long-press gesture and Reconnect lives in Settings, so
   * neither needs a row; the retired ids stay handled in EventRouter because
   * a host may still be showing a cached menu.
   */
  private systemMenu(): MenuContainerProperty {
    return new MenuContainerProperty({
      menuItems: CAPABILITIES.map((name, index) => new MenuItemProperty({
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
      paddingLength: 8,
      borderWidth: 0,
      isEventCapture: 1,
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
      }), new TextContainerProperty({
        // The state word, natively. Above every tile (larger zOrderIndex is
        // in front) and never the capturer: input stays with the layer
        // behind the tiles, exactly as before.
        containerID: ContainerId.SpineLabel,
        containerName: SPINE_LABEL_NAME,
        xPosition: SPINE_LABEL_BOX.x,
        yPosition: SPINE_LABEL_BOX.y,
        width: SPINE_LABEL_BOX.width,
        height: SPINE_LABEL_BOX.height,
        paddingLength: 0,
        borderWidth: 0,
        isEventCapture: 0,
        zOrderIndex: 6,
        textColor: SPINE_LABEL_BRIGHTNESS,
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
      containerTotalNum: 6,
      ...this.visualObjects(),
      ...this.menuConfig(),
    });
  }

  visualLayout(): RebuildPageContainer {
    return new RebuildPageContainer({
      containerTotalNum: 6,
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
   * Navigation is progressive disclosure, not the application's resting shell.
   * Left double-click temporarily gives the left 160px to the firmware list;
   * the current overview remains visible, dimmed, in the remaining 412px.
   */
  listLayout(
    items: string[],
    chrome: { title?: string; hint?: string } = {},
  ): RebuildPageContainer {
    const capped = items.slice(0, 20).map((s) => s.slice(0, 64));
    const title = (chrome.title?.trim() || 'JOAT').slice(0, 46);
    const hint = (chrome.hint?.trim() || 'left x2 overview').slice(0, 46);
    return new RebuildPageContainer({
      containerTotalNum: 4,
      ...this.menuConfig(),
      // Capture moves OFF main and ONTO the list — see main()'s note.
      textObject: [
        this.statusBar(),
        this.main(40, 0, 4, title),
        this.region(ContainerId.Hint, 'hint', 256, 32, 4, 1, hint),
      ],
      listObject: [
        new ListContainerProperty({
          containerID: ContainerId.List,
          containerName: 'list',
          xPosition: 0,
          yPosition: 72,
          width: CANVAS.width,
          height: 184,
          paddingLength: 6,
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

  /** Dedicated decision surface: one bright question, one horizontal CTA row. */
  confirmationLayout(prompt: string): RebuildPageContainer {
    const safePrompt = (prompt.trim() || 'Confirm requested action.').slice(0, 120);
    return new RebuildPageContainer({
      containerTotalNum: 5,
      ...this.menuConfig(),
      textObject: [
        this.statusBar(),
        this.main(144, 1, 4, `CONFIRM ACTION\n\n${safePrompt}`),
        this.region(
          ContainerId.Context, 'context', 176, 48, 3, 3,
          'TAP · confirm   SWIPE · cancel',
        ),
        this.region(ContainerId.Tray, 'tray', 224, 32, 4, 3, ''),
        this.region(ContainerId.Hint, 'hint', 256, 32, 5, 1, '8 seconds · timeout cancels'),
      ],
    });
  }
}
