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

  private main(height: number): TextContainerProperty {
    return new TextContainerProperty({
      containerID: ContainerId.Main,
      containerName: 'main',
      xPosition: 0,
      yPosition: 32,
      width: CANVAS.width,
      height,
      paddingLength: 8,
      borderWidth: 0,
      // The single event-capture container for every layout.
      isEventCapture: 1,
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
      textObject: [this.statusBar(), this.main(48)],
      listObject: [
        new ListContainerProperty({
          containerID: ContainerId.List,
          containerName: 'list',
          xPosition: 0,
          yPosition: 80,
          width: CANVAS.width,
          height: CANVAS.height - 80,
          paddingLength: 6,
          isEventCapture: 0,
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
