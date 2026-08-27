import {
  CreateStartUpPageContainer,
  RebuildPageContainer,
  TextContainerProperty,
  ListContainerProperty,
  ListItemContainerProperty,
  MenuContainerProperty,
  MenuItemProperty,
} from '@evenrealities/even_hub_sdk';
import { CANVAS, ContainerId } from './types';

/**
 * Declares HUD layouts against the 576x288 canvas.
 *
 * Invariants enforced here so they can never drift:
 *  - exactly one container carries isEventCapture: 1
 *  - zOrderIndex is set on every container and is unique (all-or-nothing rule)
 *  - containerTotalNum matches the number of declared containers
 */
export class HudPageBuilder {
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
  ): TextContainerProperty {
    return new TextContainerProperty({
      containerID: ContainerId.Main,
      containerName: 'main',
      xPosition: 0,
      yPosition: 32,
      width: CANVAS.width,
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
  ): TextContainerProperty {
    return new TextContainerProperty({
      containerID: id,
      containerName: name,
      xPosition: 0,
      yPosition: y,
      width: CANVAS.width,
      height,
      paddingLength: 8,
      borderWidth: 0,
      isEventCapture: 0,
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
   * reach from ANY screen without costing the display. Attached to EVERY
   * layout deliberately — a rebuild without menuObject CLEARS the menu, so
   * closing settings would silently delete it (SDK d.ts on rebuild).
   */
  static readonly MENU_REPEAT = 1;
  static readonly MENU_MUTE = 2;
  static readonly MENU_REDIAL = 3;

  private systemMenu(): MenuContainerProperty {
    return new MenuContainerProperty({
      menuItems: [
        new MenuItemProperty({ itemID: HudPageBuilder.MENU_REPEAT, itemName: 'Repeat last reply' }),
        new MenuItemProperty({ itemID: HudPageBuilder.MENU_MUTE, itemName: 'Mute / unmute mic' }),
        new MenuItemProperty({ itemID: HudPageBuilder.MENU_REDIAL, itemName: 'Reconnect brain' }),
      ],
    });
  }

  /** Status bar + main text. The default for voice → HUD replies. */
  startup(): CreateStartUpPageContainer {
    return new CreateStartUpPageContainer({
      containerTotalNum: 5,
      textObject: this.dashboard(),
      menuObject: this.systemMenu(),
    });
  }

  /** The same five regions, for rebuildPageContainer. */
  defaultLayout(): RebuildPageContainer {
    return new RebuildPageContainer({
      containerTotalNum: 5,
      textObject: this.dashboard(),
      menuObject: this.systemMenu(),
    });
  }

  /**
   * Full-canvas focus page: status + named title + rows + truthful actions.
   *
   * The focus header carries its action copy because a separate 32px footer
   * leaves room for only four firmware list rows. HudPresenter buffers every
   * dashboard-only paint while this layout is active, so missing optional
   * containers can never be mistaken for page corruption.
   */
  listLayout(
    items: string[],
    chrome: { title?: string; hint?: string } = {},
  ): RebuildPageContainer {
    const capped = items.slice(0, 20).map((s) => s.slice(0, 64));
    const header = this.focusHeader(chrome.title, chrome.hint);
    return new RebuildPageContainer({
      containerTotalNum: 3,
      menuObject: this.systemMenu(),
      // Capture moves OFF main and ONTO the list — see main()'s note.
      textObject: [
        this.statusBar(),
        this.main(48, 0, 3, header),
      ],
      listObject: [
        new ListContainerProperty({
          containerID: ContainerId.List,
          containerName: 'list',
          xPosition: 0,
          yPosition: 80,
          width: CANVAS.width,
          height: CANVAS.height - 80,
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

  /** Keep the action copy intact; title truncation is safer than hiding Back. */
  private focusHeader(title = '', hint = ''): string {
    const action = hint.trim().slice(0, 46);
    const name = title.trim().slice(0, 46);
    if (!action) return name;
    if (!name) return action;
    const nameBudget = 46 - action.length - 3; // ` · `
    if (nameBudget <= 0) return action;
    return `${name.slice(0, nameBudget)} · ${action}`;
  }
}
