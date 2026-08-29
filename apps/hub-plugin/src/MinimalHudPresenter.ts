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
  private assistant?: string;
  private assistantTimer?: ReturnType<typeof setTimeout>;

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
    return this.paintComposed(textColor, contentKind);
  }

  override async appendText(
    chunk: string,
    containerId: ContainerId = ContainerId.Main,
  ): Promise<boolean> {
    if (containerId === ContainerId.Main) this.main += chunk;
    else if (containerId === ContainerId.StatusBar) this.status += chunk;
    else this.hint += chunk;
    return this.paintComposed();
  }

  override setStatus(text: string): Promise<boolean> {
    if (this.onStatus) {
      this.onStatus(text);
      return Promise.resolve(true);
    }
    return this.showText(text, ContainerId.StatusBar);
  }

  /** The recovery surface has no image layer, so compose the transient reply
   * above its retained content rather than silently dropping the answer. */
  override async showAssistantOverlay(text: string, append = false): Promise<boolean> {
    if (!text) return false;
    if (this.assistantTimer) clearTimeout(this.assistantTimer);
    this.assistant = append ? `${this.assistant ?? ''}${text}` : text;
    return this.paintComposed();
  }

  override settleAssistantOverlay(dismissAfterMs: number): boolean {
    if (!this.assistant || !Number.isInteger(dismissAfterMs) || dismissAfterMs < 1_000 || dismissAfterMs > 15_000) {
      return false;
    }
    if (this.assistantTimer) clearTimeout(this.assistantTimer);
    this.assistantTimer = setTimeout(() => void this.dismissAssistantOverlay(), dismissAfterMs);
    return true;
  }

  override async dismissAssistantOverlay(): Promise<boolean> {
    if (this.assistantTimer) clearTimeout(this.assistantTimer);
    this.assistantTimer = undefined;
    this.assistant = undefined;
    return this.paintComposed();
  }

  override hasAssistantOverlay(): boolean {
    return this.assistant !== undefined;
  }

  private paintComposed(textColor?: number, contentKind?: HudContentKind): Promise<boolean> {
    return super.showText(this.composed(), ContainerId.Main, textColor, contentKind);
  }

  private composed(): string {
    const base = `${this.status}\n\n${this.main}\n\n${this.hint}`;
    return this.assistant ? `JOAT\n\n${this.assistant}\n\n${base}` : base;
  }
}
