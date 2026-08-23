import { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { HudPageBuilder } from './HudPageBuilder';
import { ContainerId } from './types';

const MAX_UPGRADE_CHARS = 2000;

/**
 * Renders text to the HUD.
 *
 * textContainerUpgrade is the default path — it updates in place with no
 * flicker, which matters when streaming a reply token-by-token.
 * rebuildPageContainer is reserved for actual layout changes.
 */
export class HudPresenter {
  private lastLength = new Map<number, number>();

  constructor(
    private readonly client: BridgeClient,
    private readonly layouts: HudPageBuilder,
  ) {}

  /** Replace a container's full text. */
  async showText(text: string, containerId: ContainerId = ContainerId.Main): Promise<boolean> {
    const content = text.slice(0, MAX_UPGRADE_CHARS);
    const previous = this.lastLength.get(containerId) ?? 0;
    const ok = await this.client.require().textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: containerId,
        contentOffset: 0,
        // Overwrite whatever was there before, otherwise stale tail text survives.
        contentLength: Math.max(previous, content.length),
        content,
      }),
    );
    this.lastLength.set(containerId, content.length);
    return ok;
  }

  /** Append without redrawing — for streaming LLM output. */
  async appendText(chunk: string, containerId: ContainerId = ContainerId.Main): Promise<boolean> {
    const offset = this.lastLength.get(containerId) ?? 0;
    if (offset + chunk.length > MAX_UPGRADE_CHARS) {
      // Ran out of container budget: restart from the tail of the stream.
      return this.showText(chunk, containerId);
    }
    const ok = await this.client.require().textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: containerId,
        contentOffset: offset,
        contentLength: chunk.length,
        content: chunk,
      }),
    );
    this.lastLength.set(containerId, offset + chunk.length);
    return ok;
  }

  setStatus(text: string): Promise<boolean> {
    return this.showText(text, ContainerId.StatusBar);
  }

  async showList(items: string[]): Promise<boolean> {
    const ok = await this.client.require().rebuildPageContainer(this.layouts.listLayout(items));
    this.lastLength.clear();
    return ok;
  }

  async showDefault(): Promise<boolean> {
    const ok = await this.client.require().rebuildPageContainer(this.layouts.defaultLayout());
    this.lastLength.clear();
    return ok;
  }
}
