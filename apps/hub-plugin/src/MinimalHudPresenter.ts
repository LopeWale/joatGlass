import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import { ContainerId, type HudContentKind } from './types';

/**
 * Last-resort presenter for hosts that accept only the one-container startup.
 *
 * This is recovery infrastructure, not a product surface. It keeps the Mac
 * link, audio, and input router alive while flattening plugin chrome and the
 * latest reply into the sole known-visible container. Rich flows remain on
 * the modern four-tile renderer and are not accepted from this fallback.
 */
export class MinimalHudPresenter extends HudPresenter {
  private status = 'JOAT · compatibility mode';
  private main = 'JOAT is running with a reduced display.';
  private hint = 'Keep the phone foregrounded. Reopen JOAT to retry the visual HUD.';

  constructor(client: BridgeClient, layouts: HudPageBuilder) {
    super(client, layouts);
  }

  override async showText(
    text: string,
    containerId: ContainerId = ContainerId.Main,
    textColor?: number,
    contentKind?: HudContentKind,
  ): Promise<boolean> {
    const clean = text.trim();
    if (containerId === ContainerId.StatusBar) this.status = clean;
    else if (containerId === ContainerId.Hint) this.hint = clean;
    else this.main = clean;
    return super.showText(this.composed(), ContainerId.Main, textColor, contentKind);
  }

  override async appendText(
    chunk: string,
    containerId: ContainerId = ContainerId.Main,
  ): Promise<boolean> {
    if (containerId === ContainerId.Main) this.main += chunk;
    else if (containerId === ContainerId.StatusBar) this.status += chunk;
    else this.hint += chunk;
    return super.showText(this.composed(), ContainerId.Main);
  }

  override setStatus(text: string): Promise<boolean> {
    if (this.onStatus) {
      this.onStatus(text);
      return Promise.resolve(true);
    }
    return this.showText(text, ContainerId.StatusBar);
  }

  private composed(): string {
    return `${this.status}\n\n${this.main}\n\n${this.hint}`;
  }
}
