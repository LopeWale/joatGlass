import type { MacLink } from './MacLink';
import type { AgentTerminalHudModel, TerminalAction } from './types';

type TerminalLink = Pick<MacLink, 'sendTerminal'>;

export interface AcceptedTerminalAction {
  action: TerminalAction;
  surface: 'agent_terminal';
}

/**
 * Owns wearable gesture intent while the Agent Terminal session surface is on
 * the lenses, translating taps, swipes, and holds into bounded `terminal.action`
 * messages without ever replaying stale controls across a reconnect.
 */
export class TerminalController {
  private model?: AgentTerminalHudModel;
  private suspended = false;
  private acceptedAction?: AcceptedTerminalAction;

  constructor(private readonly link: TerminalLink) {}

  present(model: AgentTerminalHudModel): void {
    this.model = {
      ...model,
      lanes: [...model.lanes],
      messages: model.messages?.map((message) => ({ ...message })),
    };
  }

  clear(): void {
    this.model = undefined;
  }

  isActive(): boolean {
    return this.model !== undefined && !this.suspended;
  }

  /** The OS contextual menu temporarily owns input; retain the underlying model
   * only until a real surface departure clears it. */
  setSuspended(suspended: boolean): void {
    this.suspended = suspended;
  }

  /** Read once after the fixed action was accepted by the live MacLink. */
  consumeAcceptedAction(): AcceptedTerminalAction | undefined {
    const accepted = this.acceptedAction;
    this.acceptedAction = undefined;
    return accepted;
  }

  /** Tap on the Agent Terminal card:
   * - `approvals > 0`: approve pending tool call (`approve_terminal`)
   * - `running`: consume tap to keep active session visible
   * - `blocked | unavailable | approval_required`: retry connection (`retry_terminal`)
   * - `ready | settled | cancelled | failed`: start voice input (`begin_terminal_input`)
   */
  handleClick(): boolean {
    const model = this.model;
    if (model === undefined || this.suspended) return false;
    if ((model.approvals ?? 0) > 0) {
      return this.send('approve_terminal');
    }
    if (model.state === 'running') {
      return true;
    }
    if (
      model.state === 'blocked'
      || model.state === 'approval_required'
      || model.state === 'unavailable'
    ) {
      return this.send('retry_terminal');
    }
    if (
      model.state === 'ready'
      || model.state === 'settled'
      || model.state === 'cancelled'
      || model.state === 'failed'
    ) {
      return this.send('begin_terminal_input');
    }
    return false;
  }

  /** Hold (long press) on a running session stops the in-flight turn (`cancel_terminal`).
   * Consumed even if the link is down so a cancel gesture never mutes the mic instead. */
  handleLongPress(): boolean {
    const model = this.model;
    if (model === undefined || this.suspended) return false;
    if (model.state !== 'running') return false;
    this.send('cancel_terminal');
    return true;
  }

  /** Swipe denies a pending tool approval (`deny_terminal`); otherwise returns false
   * so EventRouter scrolls the conversation viewport. */
  handleSwipe(): boolean {
    if (this.suspended || this.model === undefined) return false;
    if ((this.model.approvals ?? 0) > 0) {
      return this.send('deny_terminal');
    }
    return false;
  }

  /** Double-tap (`2x: sessions`) is owned by CapabilityMenu Back navigation
   * (or PromptInputOwner while listening/transcribing). */
  handleCancel(): boolean {
    return false;
  }

  private send(action: TerminalAction): boolean {
    const accepted = this.link.sendTerminal({ kind: 'terminal.action', action });
    if (accepted && this.model) {
      this.acceptedAction = { action, surface: 'agent_terminal' };
    }
    return accepted;
  }
}
