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
  private acceptedAction?: AcceptedBrainstormAction;
  private acceptancePending = false;
  private acceptanceStartPending = false;

  constructor(private readonly link: BrainstormLink) {}

  present(model: BrainstormHudModel): void {
    this.model = { ...model, collaborators: [...model.collaborators] };
    // acceptance_ready is an explicit Mac acknowledgement. Keep the trigger
    // consumed while that phase is visible, and accept exactly one start tap.
    this.acceptancePending = model.state === 'acceptance_ready';
    this.acceptanceStartPending = false;
  }

  clear(): void {
    this.model = undefined;
    this.acceptancePending = false;
    this.acceptanceStartPending = false;
  }

  isActive(): boolean {
    return this.model !== undefined;
  }

  /** The Mac must explicitly arm the isolated diagnostic route. Absence is
   * deliberately false so an older or ordinary engine can never expose a
   * hidden right/ring acceptance gesture. */
  isAcceptanceArmed(): boolean {
    const model = this.model;
    // Brainstorm facts only. This also required terminalState === 'ready'
    // and noToolsChecked — fields a Brainstorm frame no longer carries,
    // because gating a Brainstorm arm on TERMINAL readiness is exactly the
    // coupling docs/SESSION_MODEL.md:74-75 forbids. Reading them off a
    // split frame silently disarmed the diagnostic: the double press did
    // nothing and said nothing, which the acceptance flow caught.
    //
    // The Mac remains the authority. `acceptanceArmed` is an explicit
    // local opt-in that is off by default, and the acceptance controller
    // refuses its own run when the terminal cannot carry it — the check
    // belongs there, where the terminal's state actually lives.
    return model?.acceptanceArmed === true
      && model.screen === 'brainstorm'
      && model.state === 'ready';
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
    if (model === undefined) return false;
    if (model.screen === 'brainstorm' && model.state === 'acceptance_ready') {
      if (this.acceptanceStartPending) return true;
      const accepted = this.send('begin_topic_capture');
      if (accepted) this.acceptanceStartPending = true;
      // Never let a disconnected acceptance tap leak into another surface.
      return true;
    }
    if (this.acceptancePending) return true;

    if (model.screen === 'brainstorm') {
      // The Mac has armed exactly one next utterance. A second tap must not
      // re-arm or queue a duplicate capture while that promise is visible.
      if (model.state === 'topic_capture') return true;
      if (model.state === 'ready') return this.send('begin_topic_capture');
      if (model.state === 'running') return this.send('show_terminal');
      return false;
    }

    if (model.state === 'running') return this.send('show_brainstorm');
    // A tap on the TERMINAL arms terminal input. It used to send
    // begin_topic_capture — i.e. the Agent Terminal's primary gesture
    // started a Brainstorm, a different feature with different
    // collaborators.
    if (model.terminalState === 'ready') return this.send('begin_terminal_input');
    // A waiting APPROVAL is the decision in front of the wearer, so the tap
    // answers it. This used to send retry_terminal — retrying the daemon
    // when what was blocked was a tool request — and the only real way to
    // answer was to remember the words "terminal approve".
    // A pending TOOL request, not a connection waiting on device approval
    // in OpenClaw. Both once read `approval_required`, and the remedies are
    // opposite: approve the tool, or retry the daemon.
    if ((model.approvals ?? 0) > 0) {
      return this.send('approve_terminal');
    }
    // approval_required stays here. With no pending tool request it means
    // the CONNECTION is waiting on device approval in OpenClaw, and the
    // remedy is still a retry — dropping it from this list made that state
    // do nothing at all, which the acceptance flow caught.
    if (
      model.terminalState === 'blocked'
      || model.terminalState === 'approval_required'
      || model.terminalState === 'unavailable'
    ) {
      return this.send('retry_terminal');
    }
    return false;
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
    return this.model?.screen === 'brainstorm'
      && (this.model.state === 'running' || this.model.state === 'topic_capture')
      ? this.send('cancel')
      : false;
  }

  /** Isolated right-temple/ring double press from an explicitly armed surface.
   * The fixed metadata proves the initiating G2 input without carrying a
   * topic, prompt, endpoint, or any OpenClaw payload. */
  handleAcceptance(source: 'right' | 'ring'): boolean {
    const model = this.model;
    if (this.acceptancePending || model?.state === 'acceptance_ready') return true;
    if (!this.isAcceptanceArmed()) return false;
    const action: BrainstormAction = 'begin_acceptance';
    const accepted = this.link.sendBrainstorm({
      kind: 'brainstorm.action',
      action,
      surface: 'brainstorm',
      source,
      gesture: 'double_press',
    });
    if (accepted) {
      this.acceptancePending = true;
      this.acceptedAction = { action, surface: 'brainstorm' };
    }
    // An eligible deliberate test gesture is always consumed locally. A
    // reconnect must never turn it into the platform exit dialog.
    return true;
  }

  private send(action: Exclude<BrainstormAction, 'begin_acceptance'>): boolean {
    // Never queue a stale G2 control across a reconnect. The current Mac
    // session must authenticate and receive the action while it is current.
    const accepted = this.link.sendBrainstorm({ kind: 'brainstorm.action', action });
    if (accepted && this.model) {
      this.acceptedAction = { action, surface: this.model.screen };
    }
    return accepted;
  }
}
