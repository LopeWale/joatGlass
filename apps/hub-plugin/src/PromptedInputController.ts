import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import type { AudioStreamer, PromptMicrophone } from './AudioStreamer';
import type { HudPresenter } from './HudPresenter';
import type { DownlinkContext, MacLink } from './MacLink';
import type { PromptAckMessage, PromptCancelMessage, PromptPresentMessage } from './types';

interface PromptedInputOptions {
  presenter: Partial<Pick<HudPresenter, 'showPromptQuestion' | 'dismissPromptQuestion'>>;
  audio: Pick<AudioStreamer, 'openPromptMicrophone' | 'activatePromptEpoch' | 'clearPromptEpoch'>;
  link: Pick<MacLink, 'sendForGeneration'>;
  monotonicMs?: () => number;
  isOwnerCurrent?: (message: Readonly<PromptPresentMessage>) => boolean;
  onCancel?: (message: Readonly<PromptPresentMessage>, reason?: string) => void;
}

interface Publication {
  readonly message: Readonly<PromptPresentMessage>;
  readonly context: DownlinkContext;
  readonly deadline: number;
  readonly abort: () => void;
  timer?: ReturnType<typeof setTimeout>;
  started: boolean;
  microphone?: PromptMicrophone;
}

const PRESENT_KEYS = [
  'kind', 'promptId', 'question', 'presentationBinding', 'connectionGeneration',
  'source', 'expiresInMs', 'ownerKind', 'ownerId', 'field',
] as const;
const CANCEL_KEYS = ['kind', 'promptId', 'connectionGeneration'] as const;

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length
    && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function token(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}

function ownerIdentifier(value: unknown, maximum: number): value is string {
  return typeof value === 'string' && value.length >= 1 && value.length <= maximum
    && /^[A-Za-z0-9._:-]+$/.test(value);
}

function question(value: unknown): value is string {
  if (typeof value !== 'string' || !value.trim() || value.length > 1000 || value.includes('\0')) return false;
  // Reject lone UTF-16 surrogates. Preserve valid pairs and every literal
  // character; validation does not trim or rewrite the displayed question.
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(++index);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return false;
  }
  return true;
}

/** Runtime validation mirrors the Mac prompt protocol; malformed input cannot
 * evict a valid question. Copy fields before any asynchronous work begins. */
function parse(value: unknown): PromptPresentMessage | PromptCancelMessage | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return;
  const data = value as Record<string, unknown>;
  if (!token(data.promptId) || !token(data.connectionGeneration)) return;
  if (data.kind === 'prompt.cancel' && exactKeys(data, CANCEL_KEYS)) {
    return { kind: data.kind, promptId: data.promptId, connectionGeneration: data.connectionGeneration };
  }
  if (data.kind !== 'prompt.present' || !exactKeys(data, PRESENT_KEYS)
    || !question(data.question)
    || typeof data.presentationBinding !== 'string' || !/^[0-9a-f]{64}$/.test(data.presentationBinding)
    || (data.source !== 'glasses' && data.source !== 'phone')
    || typeof data.expiresInMs !== 'number' || !Number.isInteger(data.expiresInMs)
    || data.expiresInMs < 1 || data.expiresInMs > 45000
    || (data.ownerKind !== 'messages' && data.ownerKind !== 'brainstorm'
      && data.ownerKind !== 'terminal' && data.ownerKind !== 'capability')
    || !ownerIdentifier(data.ownerId, 256) || !ownerIdentifier(data.field, 64)) return;
  return {
    kind: data.kind, promptId: data.promptId, question: data.question,
    presentationBinding: data.presentationBinding, connectionGeneration: data.connectionGeneration,
    source: data.source, expiresInMs: data.expiresInMs, ownerKind: data.ownerKind,
    ownerId: data.ownerId, field: data.field,
  };
}

function sameContext(left: DownlinkContext, right: DownlinkContext): boolean {
  return left.generation === right.generation && left.signal === right.signal;
}

function validContext(context: DownlinkContext): boolean {
  return Number.isSafeInteger(context.generation) && context.generation > 0
    && context.signal !== undefined && !context.signal.aborted;
}

/** One question owns display and fresh audio admission. Wire observation is
 * synchronous so cancellation invalidates a pending paint immediately; the
 * ordinary downlink dispatcher still serializes all actual presentation work. */
export class PromptedInputController {
  private current?: Publication;
  private latestContext?: DownlinkContext;
  private readonly now: () => number;

  constructor(private readonly options: PromptedInputOptions) {
    this.now = options.monotonicMs ?? (() => performance.now());
  }

  observe(value: unknown, context: DownlinkContext): void {
    const message = parse(value);
    if (!message || !validContext(context)) return;
    const latest = this.latestContext;
    if (latest && (context.generation < latest.generation
      || (context.generation === latest.generation && !sameContext(context, latest)))) return;
    if (message.kind === 'prompt.cancel') {
      const current = this.current;
      if (current && sameContext(current.context, context)
        && current.message.promptId === message.promptId
        && current.message.connectionGeneration === message.connectionGeneration) {
        this.retire(current, false);
      }
      return;
    }

    if (!this.ownerIsCurrent(message)) return;

    const previous = this.current;
    // A current ID cannot be redefined or renewed, including a duplicate
    // received while the original is still waiting in the downlink queue.
    if (previous?.message.promptId === message.promptId) return;

    const captured = Object.freeze({ generation: context.generation, signal: context.signal });
    const publication: Publication = {
      message: Object.freeze(message), context: captured,
      deadline: this.now() + message.expiresInMs, started: false,
      abort: () => this.retire(publication, false),
    };
    this.latestContext = captured;
    this.current = publication;
    // Install the successor identity first. Old cleanup is scoped to its own
    // ID and cannot accidentally clear a replacement from a reentrant hook.
    if (previous) this.cleanup(previous, true);
    if (this.current !== publication) return;
    context.signal.addEventListener('abort', publication.abort, { once: true });
    publication.timer = setTimeout(() => this.retire(publication, true), message.expiresInMs);
    if (context.signal.aborted) this.retire(publication, false);
  }

  async handle(value: unknown, context: DownlinkContext): Promise<void> {
    const message = parse(value);
    const publication = this.current;
    if (!message || message.kind !== 'prompt.present' || !publication
      || !sameContext(publication.context, context)
      || !PRESENT_KEYS.every((key) => publication.message[key] === message[key])
      || publication.started || !this.isCurrent(publication)) return;
    publication.started = true;
    let ackAttempted = false;
    try {
      const show = this.options.presenter.showPromptQuestion;
      const painted = show
        ? await show.call(this.options.presenter, message.promptId, message.question, () => this.isCurrent(publication), message.ownerKind)
        : false;
      if (!this.isCurrent(publication)) return;
      if (!painted) {
        this.fail(publication);
        return;
      }
      publication.microphone = this.options.audio.openPromptMicrophone(
        message.source === 'glasses' ? AudioInputSource.Glasses : AudioInputSource.Phone,
        message.promptId,
      );
      const ready = await publication.microphone.ready;
      if (!this.isCurrent(publication)) return;
      if (!ready) {
        this.fail(publication);
        return;
      }
      const activated = this.options.audio.activatePromptEpoch({
        promptId: message.promptId, generation: publication.context.generation,
        source: message.source === 'glasses' ? AudioInputSource.Glasses : AudioInputSource.Phone,
      }, () => {
        if (!this.isCurrent(publication)) return false;
        ackAttempted = true;
        return this.send(publication, {
          ...this.ack(publication, true), audioEpoch: message.promptId,
        });
      });
      if (!activated && this.current === publication) {
        // Once a success receipt was attempted, do not issue a contradictory
        // second receipt or retry it on another connection.
        if (ackAttempted) this.retire(publication, false);
        else this.fail(publication);
      }
    } catch {
      // No question, transcript or exception text belongs in diagnostics.
      if (this.current === publication) {
        if (ackAttempted) this.retire(publication, false);
        else this.fail(publication);
      }
    }
  }

  isActive(): boolean {
    const publication = this.current;
    return publication !== undefined && this.isCurrent(publication);
  }

  cancel(reason?: string, notify = true): void {
    const publication = this.current;
    if (!publication) return;
    this.retire(publication, notify);
    // Retire first: this synchronous callback may cancel an application
    // action or install a successor and must never inherit the old mic.
    try { this.options.onCancel?.(publication.message, reason); } catch { /* Ownership is already revoked. */ }
  }

  /** Main owns the AudioStreamer callback and forwards the exact invalidated ID. */
  audioInvalidated(promptId: string): void {
    const publication = this.current;
    if (publication?.message.promptId === promptId) this.retire(publication, true);
  }

  private isCurrent(publication: Publication): boolean {
    if (this.current !== publication) return false;
    if (publication.context.signal.aborted) {
      this.retire(publication, false);
      return false;
    }
    if (this.now() >= publication.deadline) {
      this.retire(publication, true);
      return false;
    }
    if (!this.ownerIsCurrent(publication.message)) {
      this.retire(publication, true);
      return false;
    }
    return true;
  }

  private ownerIsCurrent(message: Readonly<PromptPresentMessage>): boolean {
    try { return this.options.isOwnerCurrent?.(message) ?? true; } catch { return false; }
  }

  private ack(publication: Publication, success: boolean): PromptAckMessage {
    const message = publication.message;
    return {
      kind: 'prompt.ack', promptId: message.promptId,
      presentationBinding: message.presentationBinding,
      connectionGeneration: message.connectionGeneration, source: message.source, success,
    };
  }

  private send(publication: Publication, message: PromptAckMessage | PromptCancelMessage): boolean {
    if (publication.context.signal.aborted) return false;
    try {
      return this.options.link.sendForGeneration(message, publication.context.generation) === true;
    } catch { return false; }
  }

  private fail(publication: Publication): void {
    if (!this.isCurrent(publication)) return;
    this.send(publication, this.ack(publication, false));
    this.retire(publication, false);
  }

  private retire(publication: Publication, notify: boolean): void {
    if (this.current !== publication) return;
    this.current = undefined;
    this.cleanup(publication, notify);
  }

  private cleanup(publication: Publication, notify: boolean): void {
    if (publication.timer !== undefined) clearTimeout(publication.timer);
    publication.context.signal.removeEventListener('abort', publication.abort);
    publication.microphone?.release();
    try { this.options.audio.clearPromptEpoch(publication.message.promptId); } catch { /* Already revoked locally. */ }
    try {
      const dismissed = this.options.presenter.dismissPromptQuestion?.(publication.message.promptId);
      void dismissed?.catch(() => false);
    } catch { /* A host display failure cannot restore input ownership. */ }
    if (notify) this.send(publication, {
      kind: 'prompt.cancel', promptId: publication.message.promptId,
      connectionGeneration: publication.message.connectionGeneration,
    });
  }
}
