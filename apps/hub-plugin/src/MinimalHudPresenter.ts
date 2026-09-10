import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import { validAssistantConversationSnapshot } from './AssistantConversationSurface';
import {
  ContainerId,
  type AssistantConversationSnapshot,
  type HudContentKind,
} from './types';

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
  private conversation?: AssistantConversationSnapshot;
  private conversationPrompt = false;
  private conversationTimer?: ReturnType<typeof setTimeout>;

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
    this.clearConversationTimer();
    this.conversation = undefined;
    this.conversationPrompt = false;
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

  override async showAssistantConversation(snapshot: AssistantConversationSnapshot): Promise<boolean> {
    if (!validAssistantConversationSnapshot(snapshot)) return false;
    const current = this.conversation;
    if (current?.conversationId === snapshot.conversationId
      && snapshot.revision < current.revision) return false;
    this.conversation = {
      ...snapshot,
      turns: snapshot.turns.map((turn) => ({ ...turn })),
    };
    this.conversationPrompt = false;
    this.assistant = undefined;
    if (this.assistantTimer) clearTimeout(this.assistantTimer);
    this.assistantTimer = undefined;
    this.clearConversationTimer();
    const painted = await this.paintComposed();
    if (painted) this.armConversationTimer();
    return painted;
  }

  override hasAssistantConversation(): boolean {
    return this.conversation !== undefined;
  }

  override assistantConversationIdentity(): { conversationId: string; revision: number } | undefined {
    const current = this.conversation;
    return current
      ? { conversationId: current.conversationId, revision: current.revision }
      : undefined;
  }

  override isAssistantConversationContinuePromptVisible(): boolean {
    return this.conversation !== undefined && this.conversationPrompt;
  }

  override scrollAssistantConversation(_direction: 'up' | 'down'): boolean {
    // Compatibility mode owns the gesture even though its one text
    // container cannot expose the rich transcript viewport.
    return this.conversation !== undefined;
  }

  override async continueAssistantConversation(): Promise<boolean> {
    if (!this.conversation || !this.conversationPrompt) return false;
    this.conversationPrompt = false;
    const painted = await this.paintComposed();
    if (!painted) {
      this.conversationPrompt = true;
      return false;
    }
    this.armConversationTimer();
    return true;
  }

  override async requestAssistantConversationExit(): Promise<'inactive' | 'prompted' | 'exited' | 'failed'> {
    if (!this.conversation) return 'inactive';
    if (!this.conversationPrompt) {
      this.conversationPrompt = true;
      this.clearConversationTimer();
      return await this.paintComposed() ? 'prompted' : 'failed';
    }
    this.conversation = undefined;
    this.conversationPrompt = false;
    this.clearConversationTimer();
    return await this.paintComposed() ? 'exited' : 'failed';
  }

  override async dismissAssistantConversation(
    conversationId: string,
    revision: number,
  ): Promise<boolean> {
    const current = this.conversation;
    if (!current || current.conversationId !== conversationId || current.revision !== revision) {
      return false;
    }
    this.conversation = undefined;
    this.conversationPrompt = false;
    this.clearConversationTimer();
    return this.paintComposed();
  }

  override whenAssistantConversationIdle(): Promise<void> {
    return Promise.resolve();
  }

  override suspendDisplay(): void {
    if (this.assistantTimer) clearTimeout(this.assistantTimer);
    this.assistantTimer = undefined;
    this.clearConversationTimer();
    this.assistant = undefined;
    this.conversation = undefined;
    this.conversationPrompt = false;
    super.suspendDisplay();
  }

  override repaintRestoredSurface(): Promise<boolean> {
    return this.paintComposed();
  }

  private paintComposed(textColor?: number, contentKind?: HudContentKind): Promise<boolean> {
    return super.showText(this.composed(), ContainerId.Main, textColor, contentKind);
  }

  private composed(): string {
    if (this.conversation) {
      const transcript = this.conversation.turns.map((turn) => (
        `${turn.role === 'user' ? 'YOU' : 'JOAT'} · ${turn.text}`
      )).join('\n\n');
      const footer = this.conversationPrompt
        ? 'Continue conversation? tap stays · x2 exits'
        : this.conversation.state === 'ready' ? '■' : '■ □';
      return `JOAT · compatibility conversation\n\n${transcript}\n\n${footer}`;
    }
    const base = `${this.status}\n\n${this.main}\n\n${this.hint}`;
    return this.assistant ? `JOAT\n\n${this.assistant}\n\n${base}` : base;
  }

  private armConversationTimer(): void {
    const current = this.conversation;
    if (!current || current.state !== 'ready' || this.conversationPrompt) return;
    this.clearConversationTimer();
    this.conversationTimer = setTimeout(() => {
      this.conversationTimer = undefined;
      if (this.conversation !== current) return;
      this.conversationPrompt = true;
      void this.paintComposed();
    }, current.continueAfterMs);
  }

  private clearConversationTimer(): void {
    if (this.conversationTimer) clearTimeout(this.conversationTimer);
    this.conversationTimer = undefined;
  }
}
