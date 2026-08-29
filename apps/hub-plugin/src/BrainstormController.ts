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

  constructor(private readonly link: BrainstormLink) {}

  present(model: BrainstormHudModel): void {
    this.model = { ...model, collaborators: [...model.collaborators] };
  }

  clear(): void {
    this.model = undefined;
  }

  isActive(): boolean {
    return this.model !== undefined;
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

    if (model.screen === 'brainstorm') {
      if (model.state === 'ready') return this.send('begin_topic_capture');
      if (model.state === 'running') return this.send('show_terminal');
      return false;
    }

    if (model.state === 'running') return this.send('show_brainstorm');
    if (model.terminalState === 'ready') return this.send('begin_topic_capture');
    if (
      model.terminalState === 'blocked'
      || model.terminalState === 'approval_required'
      || model.terminalState === 'unavailable'
    ) {
      return this.send('retry_terminal');
    }
    return false;
  }

  /** Left-double-click is a visible cancellation affordance during a live run. */
  handleCancel(): boolean {
    return this.model?.screen === 'brainstorm'
      && this.model.state === 'running'
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
