import {
  CreateStartUpPageContainer,
  RebuildPageContainer,
  TextContainerProperty,
  ListContainerProperty,
  ListItemContainerProperty,
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
  private main(height: number, isEventCapture: 0 | 1 = 1): TextContainerProperty {
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
      content: '',
    });
  }

  /** Status bar + main text. The default for voice → HUD replies. */
  startup(): CreateStartUpPageContainer {
    return new CreateStartUpPageContainer({
      containerTotalNum: 2,
      textObject: [this.statusBar(), this.main(CANVAS.height - 32)],
    });
  }

  /** Same two text containers, for rebuildPageContainer. */
  defaultLayout(): RebuildPageContainer {
    return new RebuildPageContainer({
      containerTotalNum: 2,
      textObject: [this.statusBar(), this.main(CANVAS.height - 32)],
    });
  }

  /** Status bar + short main + scrollable list (max 20 items, 64 chars each). */
  listLayout(items: string[]): RebuildPageContainer {
    const capped = items.slice(0, 20).map((s) => s.slice(0, 64));
    return new RebuildPageContainer({
      containerTotalNum: 3,
      // Capture moves OFF main and ONTO the list — see main()'s note.
      textObject: [this.statusBar(), this.main(48, 0)],
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
}
