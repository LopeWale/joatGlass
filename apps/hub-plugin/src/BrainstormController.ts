import type { MacLink } from './MacLink';
import type { BrainstormAction, BrainstormHudModel, BrainstormHudScreen } from './types';

type BrainstormLink = Pick<MacLink, 'sendBrainstorm'>;

export interface AcceptedBrainstormAction {
  action: BrainstormAction;
  surface: BrainstormHudScreen;
}

/**
 * Owns wearable intent only while the production Brainstorm visual surface is
 * on the lenses. Rendering remains in HudPresenter; this class makes the
 * gesture → bounded-Mac-action edge explicit and reconnect-safe.
 */
export class BrainstormController {
  private model?: BrainstormHudModel;
  private suspended = false;
  private acceptedAction?: AcceptedBrainstormAction;

  constructor(private readonly link: BrainstormLink) {}

  present(model: BrainstormHudModel): void {
    this.model = { ...model, collaborators: [...model.collaborators],
      messages: model.messages?.map(message => ({...message})) };
  }

  clear(): void {
    this.model = undefined;
  }

  isActive(): boolean {
    return this.model !== undefined && !this.suspended;
  }

  /** The OS menu temporarily owns input; retain the underlying frame only
   * until a real departure clears it. Closing the overlay cannot revive it. */
  setSuspended(suspended: boolean): void {
    this.suspended = suspended;
  }

  /** Read once after the exact fixed action was handed to the live MacLink.
   * It intentionally carries neither a topic nor any model payload. */
  consumeAcceptedAction(): AcceptedBrainstormAction | undefined {
    const accepted = this.acceptedAction;
    this.acceptedAction = undefined;
    return accepted;
  }

  /** Right-temple/ring click: make the next legitimate Brainstorm move. */
  handleClick(): boolean {
    const model = this.model;
    if (model === undefined || this.suspended) return false;
    if (model.screen === 'brainstorm') {
      // The Mac has armed exactly one next utterance. A second tap must not
      // re-arm or queue a duplicate capture while that promise is visible.
      if (model.state === 'topic_capture') return true;
      if (model.state === 'ready') return this.send('begin_topic_capture');
      if (model.state === 'running') return this.send('show_terminal');
      return false;
    }

    // A waiting APPROVAL is the decision in front of the wearer, so the
    // tap answers it — before ANY state branch, matching handleSwipe,
    // which already denies whenever approvals > 0. Checked after
    // `running`, a busy lane holding a pending tool request had swipe
    // DENY and tap bounce to the Brainstorm: two gestures on one frame,
    // one answering the approval and one leaving the screen. The approval
    // blocks the very run the wearer is watching; it outranks the state.
    if ((model.approvals ?? 0) > 0) {
      return this.send('approve_terminal');
    }
    // Keep the active conversation visible while its agent is answering.
    // Back belongs to navigation; a tap must not jump to another feature.
    if (model.state === 'running') return true;
    // A tap on the TERMINAL arms terminal input. It used to send
    // begin_topic_capture — i.e. the Agent Terminal's primary gesture
    // started a Brainstorm, a different feature with different
    // collaborators.
    // `state`, not the render model's old `terminalState` twin: main.ts
    // set that field from the SAME wire value, so the distinction was
    // fictional on every real frame and the twin has been removed.
    if (model.state === 'ready') return this.send('begin_terminal_input');
    // approval_required stays here. With no pending tool request it means
    // the CONNECTION is waiting on device approval in OpenClaw, and the
    // remedy is still a retry — dropping it from this list made that state
    // do nothing at all, which the acceptance flow caught.
    if (
      model.state === 'blocked'
      || model.state === 'approval_required'
      || model.state === 'unavailable'
    ) {
      return this.send('retry_terminal');
    }
    return false;
  }

  /** Long-press on a RUNNING work session cancels its job (2026-10-06).
   *
   * Everywhere else long-press is the microphone, so this claims it only
   * on that one page; there it is consumed even when the link is down,
   * because silently muting the mic is not what a cancel gesture means. */
  handleLongPress(): boolean {
    const model = this.model;
    if (model === undefined || this.suspended) return false;
    if (model.screen !== 'agent_terminal' || model.state !== 'running') return false;
    this.send('cancel_terminal');
    return true;
  }

  /** A swipe during TOPIC CAPTURE backs out of the armed capture.
   *
   * Everywhere else a swipe pages the transcript — the wearer asked for
   * scrollback in as many words, and a swipe that cancelled a RUNNING
   * session while they re-read a reply was the harshest possible answer
   * to "can I scroll?". Topic capture is the one state with nothing to
   * scroll: no transcript exists yet, the screen is a single promise
   * ("Speak your question"), and the wearer who swipes is saying never
   * mind — the same hands-free backout this surface always offered. */
  handleSwipe(): boolean {
    if (this.suspended) return false;
    // Deny is the swipe, opposite the tap that approves — the same
    // accept/refuse shape the confirmation gate already uses, so there is
    // nothing new to learn at the moment it matters most.
    if (
      this.model?.screen === 'agent_terminal'
      && (this.model.approvals ?? 0) > 0
    ) {
      return this.send('deny_terminal');
    }
    return this.model?.screen === 'brainstorm'
      && this.model.state === 'topic_capture'
      ? this.send('cancel')
      : false;
  }

  /** Left-double-click is a visible cancellation affordance during a live run. */
  handleCancel(): boolean {
    if (this.suspended) return false;
    return this.model?.screen === 'brainstorm'
      && (this.model.state === 'running' || this.model.state === 'topic_capture')
      ? this.send('cancel')
      : false;
  }

  private send(action: BrainstormAction): boolean {
    // Never queue a stale G2 control across a reconnect. The current Mac
    // session must authenticate and receive the action while it is current.
    const accepted = this.link.sendBrainstorm({ kind: 'brainstorm.action', action });
    if (accepted && this.model) {
      this.acceptedAction = { action, surface: this.model.screen };
    }
    return accepted;
  }
}
