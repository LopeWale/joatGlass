import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AudioStreamer, type PromptAudioEpoch } from './AudioStreamer';
import type { BridgeClient } from './BridgeClient';
import type { HudPresenter } from './HudPresenter';
import type { DownlinkContext, MacLink } from './MacLink';
import { PromptedInputController } from './PromptedInputController';
import type { PromptPresentMessage, UplinkMessage } from './types';

function present(overrides: Partial<PromptPresentMessage> = {}): PromptPresentMessage {
  return {
    kind: 'prompt.present', promptId: 'prompt_one', question: 'Who should receive the message?',
    presentationBinding: 'a'.repeat(64), connectionGeneration: 'mac_session_one',
    source: 'glasses', expiresInMs: 1000, ownerKind: 'messages',
    ownerId: 'message:one', field: 'recipient_name', ...overrides,
  };
}

function context(generation = 7) {
  const abort = new AbortController();
  return { abort, value: { generation, signal: abort.signal } satisfies DownlinkContext };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

type BoundMessage = Parameters<MacLink['sendForGeneration']>[0];
const cleanup: Array<() => void> = [];

function rig(hooks: {
  isOwnerCurrent?: (message: Readonly<PromptPresentMessage>) => boolean;
  onCancel?: (message: Readonly<PromptPresentMessage>, reason?: string) => void;
} = {}) {
  const defaultContext = context().value;
  const sent: Array<{ message: BoundMessage; generation: number }> = [];
  const guards = new Map<string, () => boolean>();
  const state = { now: 0, open: true, accepted: true, epoch: undefined as PromptAudioEpoch | undefined };
  const presenter: Pick<HudPresenter, 'showPromptQuestion' | 'dismissPromptQuestion'> = {
    showPromptQuestion: vi.fn(async (id, _question, isCurrent) => {
      guards.set(id, isCurrent);
      return true;
    }),
    dismissPromptQuestion: vi.fn(async () => true),
  };
  const audio: Pick<AudioStreamer, 'openPromptMicrophone' | 'activatePromptEpoch' | 'clearPromptEpoch'> = {
    openPromptMicrophone: vi.fn(() => ({ ready: Promise.resolve(state.open), release: () => undefined })),
    activatePromptEpoch: vi.fn((epoch, acknowledge) => {
      if (!state.open || !acknowledge()) return false;
      state.epoch = { ...epoch };
      return true;
    }),
    clearPromptEpoch: vi.fn((id) => {
      if (id === undefined || state.epoch?.promptId === id) state.epoch = undefined;
    }),
  };
  const link: Pick<MacLink, 'sendForGeneration'> = {
    sendForGeneration: vi.fn((message, generation) => {
      sent.push({ message, generation });
      return state.accepted;
    }),
  };
  const controller = new PromptedInputController({
    presenter, audio, link, monotonicMs: () => state.now, ...hooks,
  });
  cleanup.push(() => controller.cancel('test cleanup', false));
  return {
    controller, presenter, audio, link, state, sent, guards,
    async elapse(ms: number) {
      state.now += ms;
      await vi.advanceTimersByTimeAsync(ms);
    },
    async publish(message = present(), ctx = defaultContext) {
      controller.observe(message, ctx);
      await controller.handle(message, ctx);
      return ctx;
    },
  };
}

function successes(sent: ReturnType<typeof rig>['sent']) {
  return sent.filter((entry): entry is { message: Extract<BoundMessage, { kind: 'prompt.ack' }>; generation: number } => (
    entry.message.kind === 'prompt.ack' && entry.message.success
  ));
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  for (const dispose of cleanup.splice(0)) dispose();
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe('PromptedInputController application owner binding', () => {
  it('rejects a stale application owner synchronously before it can stage a question', async () => {
    const r = rig({ isOwnerCurrent: () => false });
    await r.publish(present({ ownerId: 'messages-ui-stale' }));
    expect(r.controller.isActive()).toBe(false);
    expect(r.presenter.showPromptQuestion).not.toHaveBeenCalled();
    expect(r.audio.activatePromptEpoch).not.toHaveBeenCalled();
    expect(r.sent).toEqual([]);
  });

  it('does not let a rejected owner replace an already accepted question', async () => {
    const r = rig({ isOwnerCurrent: (message) => message.ownerId === 'messages-ui-current' });
    const accepted = present({ ownerId: 'messages-ui-current' });
    const ctx = await r.publish(accepted);
    await r.publish(present({ promptId: 'stale_question', ownerId: 'messages-ui-stale' }), ctx);
    expect(r.presenter.showPromptQuestion).toHaveBeenCalledTimes(1);
    expect(r.state.epoch?.promptId).toBe(accepted.promptId);
    expect(r.guards.get(accepted.promptId)?.()).toBe(true);
  });

  it('checks application ownership again during held paint and before fresh audio acknowledgement', async () => {
    let current = true;
    const r = rig({ isOwnerCurrent: () => current });
    const held = deferred<boolean>();
    vi.mocked(r.presenter.showPromptQuestion).mockImplementation((id, _question, guard) => {
      r.guards.set(id, guard);
      return held.promise;
    });
    const message = present({ ownerId: 'messages-ui-current' });
    const ctx = context().value;
    r.controller.observe(message, ctx);
    const work = r.controller.handle(message, ctx);
    try {
      current = false;
      expect(r.guards.get(message.promptId)?.()).toBe(false);
      held.resolve(true);
      await work;
      expect(successes(r.sent)).toEqual([]);
      expect(r.audio.activatePromptEpoch).not.toHaveBeenCalled();
      expect(r.state.epoch).toBeUndefined();
    } finally { held.resolve(false); await work; }
  });

  it('retires accepted audio when its application owner changes without treating that as explicit Back', async () => {
    let current = true;
    const onCancel = vi.fn();
    const r = rig({ isOwnerCurrent: () => current, onCancel });
    const message = present({ ownerId: 'messages-ui-current' });
    await r.publish(message);
    current = false;
    expect(r.controller.isActive()).toBe(false);
    expect(r.state.epoch).toBeUndefined();
    expect(r.audio.clearPromptEpoch).toHaveBeenCalledWith(message.promptId);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('fails closed when the application ownership accessor throws', async () => {
    const r = rig({ isOwnerCurrent: () => { throw new Error('Private owner detail'); } });
    expect(() => r.controller.observe(present(), context().value)).not.toThrow();
    expect(r.controller.isActive()).toBe(false);
    expect(r.presenter.showPromptQuestion).not.toHaveBeenCalled();
  });

  it('reports the immutable cancelled owner synchronously after retirement and preserves a reentrant successor', async () => {
    const cancelled: Array<{ ownerId: string; reason?: string; active: boolean }> = [];
    const ctx = context().value;
    const successor = present({ promptId: 'question_two', ownerId: 'messages-ui-two' });
    const r = rig({ onCancel: (message, reason) => {
      cancelled.push({ ownerId: message.ownerId, reason, active: r.controller.isActive() });
      if (reason === 'back') r.controller.observe(successor, ctx);
    } });
    const original = present({ ownerId: 'messages-ui-one' });
    await r.publish(original, ctx);
    original.ownerId = 'mutated-after-observation';
    r.controller.cancel('back');
    expect(cancelled).toEqual([{ ownerId: 'messages-ui-one', reason: 'back', active: false }]);
    expect(r.controller.isActive()).toBe(true);
    await r.controller.handle(successor, ctx);
    expect(r.state.epoch?.promptId).toBe(successor.promptId);
  });

  it('does not call explicit cancellation observers for wire cancel, expiry, or socket abort', async () => {
    const onCancel = vi.fn();
    const r = rig({ onCancel });
    const first = present();
    const ctx = context();
    await r.publish(first, ctx.value);
    r.controller.observe({ kind: 'prompt.cancel', promptId: first.promptId, connectionGeneration: first.connectionGeneration }, ctx.value);
    await r.publish(present({ promptId: 'expires' }), ctx.value);
    await r.elapse(1000);
    await r.publish(present({ promptId: 'aborted' }), ctx.value);
    ctx.abort.abort();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('contains a cancellation observer failure after retiring input', async () => {
    const r = rig({ onCancel: () => { throw new Error('Private cancellation detail'); } });
    await r.publish();
    expect(() => r.controller.cancel('back')).not.toThrow();
    expect(r.controller.isActive()).toBe(false);
    expect(r.state.epoch).toBeUndefined();
  });
});

describe('PromptedInputController staged presentation', () => {
  it('does not paint or acknowledge during observation, or handle an unobserved message', async () => {
    const r = rig();
    const message = present();
    const ctx = context().value;
    await r.controller.handle(message, ctx);
    expect(r.presenter.showPromptQuestion).not.toHaveBeenCalled();
    r.controller.observe(message, ctx);
    expect(r.controller.isActive()).toBe(true);
    expect(r.presenter.showPromptQuestion).not.toHaveBeenCalled();
    expect(r.sent).toEqual([]);
    expect(r.audio.activatePromptEpoch).not.toHaveBeenCalled();
    await r.controller.handle(message, ctx);
    expect(r.presenter.showPromptQuestion).toHaveBeenCalledOnce();
  });

  it.each(['glasses', 'phone'] as const)('acknowledges the exact completed %s question and local generation', async (source) => {
    const r = rig();
    const message = present({ question: '  Say “send”, literally.\nNo rewriting.  ', source });
    await r.publish(message);
    expect(r.presenter.showPromptQuestion).toHaveBeenCalledWith(
      message.promptId, message.question, expect.any(Function),
    );
    expect(r.sent).toEqual([{
      generation: 7,
      message: {
        kind: 'prompt.ack', promptId: message.promptId,
        presentationBinding: message.presentationBinding,
        connectionGeneration: message.connectionGeneration, source,
        success: true, audioEpoch: message.promptId,
      },
    }]);
    expect(r.state.epoch).toEqual({ promptId: message.promptId, source, generation: 7 });
  });

  it('waits for the final paint before acknowledging or activating audio', async () => {
    const r = rig();
    const held = deferred<boolean>();
    vi.mocked(r.presenter.showPromptQuestion).mockImplementation((_id, _question, current) => {
      r.guards.set('prompt_one', current);
      return held.promise;
    });
    const message = present();
    const ctx = context().value;
    r.controller.observe(message, ctx);
    const work = r.controller.handle(message, ctx);
    try {
      expect(r.guards.get(message.promptId)?.()).toBe(true);
      expect(r.sent).toEqual([]);
      expect(r.audio.activatePromptEpoch).not.toHaveBeenCalled();
      held.resolve(true);
      await work;
      expect(successes(r.sent)).toHaveLength(1);
    } finally {
      held.resolve(false);
      await work;
    }
  });

  it('takes the deadline at ingress so a queued paint cannot renew it', async () => {
    const r = rig();
    const message = present();
    const ctx = context().value;
    r.controller.observe(message, ctx);
    await r.elapse(1000);
    expect(r.controller.isActive()).toBe(false);
    await r.controller.handle(message, ctx);
    expect(r.presenter.showPromptQuestion).not.toHaveBeenCalled();
    expect(r.audio.activatePromptEpoch).not.toHaveBeenCalled();
    expect(successes(r.sent)).toEqual([]);
  });

  it('checks monotonic expiry again when a paint settles before a delayed timer callback', async () => {
    const r = rig();
    const held = deferred<boolean>();
    vi.mocked(r.presenter.showPromptQuestion).mockReturnValue(held.promise);
    const message = present();
    const ctx = context().value;
    r.controller.observe(message, ctx);
    const work = r.controller.handle(message, ctx);
    try {
      r.state.now = 1000;
      held.resolve(true);
      await work;
      expect(successes(r.sent)).toEqual([]);
      expect(r.state.epoch).toBeUndefined();
    } finally { held.resolve(false); await work; }
  });

  it('does not repaint, rearm or extend the original deadline for duplicate presents', async () => {
    const r = rig();
    const message = present();
    const ctx = await r.publish(message);
    await r.elapse(750);
    r.controller.observe({ ...message }, ctx);
    await r.controller.handle({ ...message }, ctx);
    expect(r.presenter.showPromptQuestion).toHaveBeenCalledOnce();
    expect(r.audio.activatePromptEpoch).toHaveBeenCalledOnce();
    await r.elapse(250);
    expect(r.state.epoch).toBeUndefined();
    expect(r.guards.get(message.promptId)?.()).toBe(false);
  });

  it('does not duplicate a pending paint when the same message is queued twice', async () => {
    const r = rig();
    const held = deferred<boolean>();
    vi.mocked(r.presenter.showPromptQuestion).mockReturnValue(held.promise);
    const message = present();
    const ctx = context().value;
    r.controller.observe(message, ctx);
    const first = r.controller.handle(message, ctx);
    r.controller.observe({ ...message }, ctx);
    const second = r.controller.handle({ ...message }, ctx);
    try {
      expect(r.presenter.showPromptQuestion).toHaveBeenCalledOnce();
      held.resolve(true);
      await Promise.all([first, second]);
      expect(successes(r.sent)).toHaveLength(1);
    } finally { held.resolve(false); await Promise.all([first, second]); }
  });

  it.each([
    { question: 'A changed question' }, { ownerId: 'message:two' }, { ownerKind: 'terminal' as const },
    { field: 'body' }, { source: 'phone' as const }, { presentationBinding: 'b'.repeat(64) },
    { connectionGeneration: 'mac_session_two' }, { expiresInMs: 45000 },
  ])('does not reinterpret an existing ID after an identity change: %j', async (change) => {
    const r = rig();
    const message = present();
    const ctx = await r.publish(message);
    const changed = { ...message, ...change };
    r.controller.observe(changed, ctx);
    await r.controller.handle(changed, ctx);
    expect(r.presenter.showPromptQuestion).toHaveBeenCalledOnce();
    expect(r.guards.get(message.promptId)?.()).toBe(true);
    expect(successes(r.sent)).toHaveLength(1);
  });

  it('keeps a snapshot of the staged identity instead of trusting subsequent object mutation', async () => {
    const r = rig();
    const original = present();
    const mutable = { ...original };
    const ctx = context().value;
    r.controller.observe(mutable, ctx);
    mutable.question = 'Mutated question';
    mutable.ownerId = 'another-owner';
    await r.controller.handle(mutable, ctx);
    expect(r.presenter.showPromptQuestion).not.toHaveBeenCalled();
    await r.controller.handle(original, ctx);
    expect(r.presenter.showPromptQuestion).toHaveBeenCalledWith(
      original.promptId, original.question, expect.any(Function),
    );
  });
});

describe('PromptedInputController invalidation and races', () => {
  it.each(['abort', 'mute', 'expiry'] as const)('%s during pending paint prevents late activation', async (cause) => {
    const r = rig();
    const held = deferred<boolean>();
    vi.mocked(r.presenter.showPromptQuestion).mockImplementation((id, _question, current) => {
      r.guards.set(id, current);
      return held.promise;
    });
    const message = present();
    const ctx = context();
    r.controller.observe(message, ctx.value);
    const work = r.controller.handle(message, ctx.value);
    try {
      if (cause === 'abort') ctx.abort.abort();
      else if (cause === 'mute') r.controller.cancel('mute');
      else await r.elapse(1000);
      expect(r.controller.isActive()).toBe(false);
      expect(r.guards.get(message.promptId)?.()).toBe(false);
      held.resolve(true);
      await work;
      expect(r.audio.activatePromptEpoch).not.toHaveBeenCalled();
      expect(successes(r.sent)).toEqual([]);
    } finally { held.resolve(false); await work; }
  });

  it('revokes a held paint at cancellation ingress without waiting for its queued handler', async () => {
    const r = rig();
    const held = deferred<boolean>();
    vi.mocked(r.presenter.showPromptQuestion).mockImplementation((id, _question, current) => {
      r.guards.set(id, current);
      return held.promise;
    });
    const message = present();
    const ctx = context().value;
    r.controller.observe(message, ctx);
    const work = r.controller.handle(message, ctx);
    try {
      const cancellation = { kind: 'prompt.cancel', promptId: message.promptId,
        connectionGeneration: message.connectionGeneration };
      r.controller.observe(cancellation, ctx);
      expect(r.guards.get(message.promptId)?.()).toBe(false);
      expect(r.presenter.dismissPromptQuestion).toHaveBeenCalledWith(message.promptId);
      expect(r.sent).toEqual([]); // A received cancellation is not echoed.
      held.resolve(true);
      await work;
      await r.controller.handle(cancellation, ctx);
      expect(r.audio.activatePromptEpoch).not.toHaveBeenCalled();
      expect(r.sent).toEqual([]);
    } finally { held.resolve(false); await work; }
  });

  it.each([true, false])('old paint settling %s cannot clear or acknowledge its successor', async (oldResult) => {
    const r = rig();
    const held = deferred<boolean>();
    vi.mocked(r.presenter.showPromptQuestion).mockImplementation((id, _question, current) => {
      r.guards.set(id, current);
      return id === 'prompt_one' ? held.promise : Promise.resolve(true);
    });
    const first = present();
    const next = present({ promptId: 'prompt_two', ownerId: 'message:two', question: 'What should it say?' });
    const ctx = context().value;
    r.controller.observe(first, ctx);
    const oldWork = r.controller.handle(first, ctx);
    try {
      r.controller.observe(next, ctx);
      expect(r.guards.get(first.promptId)?.()).toBe(false);
      await r.controller.handle(first, ctx); // Old queued work is inert.
      held.resolve(oldResult);
      await oldWork;
      await r.controller.handle(next, ctx);
      expect(r.state.epoch?.promptId).toBe(next.promptId);
      expect(successes(r.sent).map(({ message }) => message.promptId)).toEqual([next.promptId]);
      expect(r.presenter.dismissPromptQuestion).not.toHaveBeenCalledWith(next.promptId);
    } finally { held.resolve(false); await oldWork; }
  });

  it('abort synchronously revokes, does not notify a dead connection, and cannot clear a successor', async () => {
    const r = rig();
    const old = context();
    const first = present();
    await r.publish(first, old.value);
    old.abort.abort();
    expect(r.state.epoch).toBeUndefined();
    expect(r.guards.get(first.promptId)?.()).toBe(false);
    expect(r.sent.filter(({ message }) => message.kind === 'prompt.cancel')).toEqual([]);
    const next = present({ promptId: 'prompt_two', connectionGeneration: 'new_mac_session' });
    await r.publish(next, context(8).value);
    r.controller.observe(first, old.value);
    await r.controller.handle(first, old.value);
    expect(r.state.epoch?.promptId).toBe(next.promptId);
  });

  it('stale local generations and same-number foreign signals cannot paint or cancel the current prompt', async () => {
    const r = rig();
    const message = present();
    const ctx = await r.publish(message, context(8).value);
    for (const foreign of [context(7).value, context(8).value]) {
      r.controller.observe({ kind: 'prompt.cancel', promptId: message.promptId,
        connectionGeneration: message.connectionGeneration }, foreign);
      await r.controller.handle(message, foreign);
    }
    expect(r.guards.get(message.promptId)?.()).toBe(true);
    expect(r.state.epoch?.generation).toBe(ctx.generation);
  });

  it('matching local cancellation drops the epoch and notifies only its captured generation once', async () => {
    const r = rig();
    const message = present();
    await r.publish(message);
    r.controller.cancel('Back');
    r.controller.cancel('Home');
    expect(r.state.epoch).toBeUndefined();
    expect(r.sent.filter(({ message }) => message.kind === 'prompt.cancel')).toEqual([{
      generation: 7, message: { kind: 'prompt.cancel', promptId: message.promptId,
        connectionGeneration: message.connectionGeneration },
    }]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('a stale audio invalidation cannot revoke a new prompt, but matching mute/source invalidation does', async () => {
    const r = rig();
    await r.publish();
    const next = present({ promptId: 'prompt_two' });
    await r.publish(next);
    r.controller.audioInvalidated('prompt_one');
    expect(r.state.epoch?.promptId).toBe(next.promptId);
    r.controller.audioInvalidated(next.promptId);
    expect(r.state.epoch).toBeUndefined();
    expect(r.guards.get(next.promptId)?.()).toBe(false);
    expect(r.sent.filter(({ message }) => message.kind === 'prompt.cancel' && message.promptId === next.promptId))
      .toHaveLength(1);
  });

  it.each([false, 'throw'] as const)('a failed paint (%s) sends a failure receipt without activation', async (result) => {
    const r = rig();
    if (result === false) vi.mocked(r.presenter.showPromptQuestion).mockResolvedValue(false);
    else vi.mocked(r.presenter.showPromptQuestion).mockRejectedValue(new Error('private question text'));
    await r.publish();
    expect(r.audio.activatePromptEpoch).not.toHaveBeenCalled();
    expect(r.sent).toEqual([{
      generation: 7, message: { kind: 'prompt.ack', promptId: 'prompt_one',
        connectionGeneration: 'mac_session_one', presentationBinding: 'a'.repeat(64),
        source: 'glasses', success: false },
    }]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('an audio activation refusal is a failure receipt, never a successful paint-only receipt', async () => {
    const r = rig();
    vi.mocked(r.audio.activatePromptEpoch).mockReturnValue(false);
    await r.publish();
    expect(successes(r.sent)).toEqual([]);
    expect(r.sent.some(({ message }) => message.kind === 'prompt.ack' && !message.success)).toBe(true);
    expect(r.state.epoch).toBeUndefined();
  });

  it.each([false, 'throw'] as const)('a rejected generation-bound ack (%s) never leaves an epoch', async (result) => {
    const r = rig();
    if (result === false) r.state.accepted = false;
    else vi.mocked(r.link.sendForGeneration).mockImplementation(() => { throw new Error('socket write failed'); });
    await expect(r.publish()).resolves.toBeDefined();
    expect(r.state.epoch).toBeUndefined();
    expect(vi.getTimerCount()).toBe(0);
    expect(r.presenter.dismissPromptQuestion).toHaveBeenCalledWith('prompt_one');
  });

  it('contains dismissal rejection during synchronous cancellation', async () => {
    const r = rig();
    await r.publish();
    vi.mocked(r.presenter.dismissPromptQuestion).mockRejectedValue(new Error('private content'));
    expect(() => r.controller.cancel('Back')).not.toThrow();
    await Promise.resolve();
    expect(r.state.epoch).toBeUndefined();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('PromptedInputController strict runtime boundary', () => {
  const invalid: Array<[string, unknown]> = [
    ['null', null], ['array', []], ['unrelated downlink', { kind: 'hud.text', text: 'ordinary' }],
    ['extra key', { ...present(), transcript: 'private' }], ['missing key', { ...present(), field: undefined }],
    ['non-token ID', { ...present(), promptId: 'prompt.one' }], ['long ID', { ...present(), promptId: 'a'.repeat(129) }],
    ['invalid session', { ...present(), connectionGeneration: 'mac session' }],
    ['uppercase binding', { ...present(), presentationBinding: 'A'.repeat(64) }],
    ['short binding', { ...present(), presentationBinding: 'a'.repeat(63) }],
    ['blank question', { ...present(), question: ' \n ' }], ['NUL question', { ...present(), question: 'a\0b' }],
    ['surrogate question', { ...present(), question: '\ud800' }],
    ['long UTF16 question', { ...present(), question: '😀'.repeat(501) }],
    ['invalid source', { ...present(), source: 'microphone' }], ['invalid owner kind', { ...present(), ownerKind: 'timer' }],
    ['blank owner', { ...present(), ownerId: '' }], ['long owner', { ...present(), ownerId: 'a'.repeat(257) }],
    ['nonliteral owner', { ...present(), ownerId: 'message one' }], ['long field', { ...present(), field: 'a'.repeat(65) }],
    ['invalid field', { ...present(), field: 'recipient name' }], ['zero TTL', { ...present(), expiresInMs: 0 }],
    ['fractional TTL', { ...present(), expiresInMs: 1.5 }], ['boolean TTL', { ...present(), expiresInMs: true }],
    ['long TTL', { ...present(), expiresInMs: 45001 }], ['NaN TTL', { ...present(), expiresInMs: NaN }],
    ['cancel extra key', { kind: 'prompt.cancel', promptId: 'prompt_one', connectionGeneration: 'mac_session_one', all: true }],
  ];
  it.each(invalid)('rejects %s without destroying a current valid question', async (_label, bad) => {
    const r = rig();
    const ctx = await r.publish();
    expect(() => r.controller.observe(bad, ctx)).not.toThrow();
    await expect(r.controller.handle(bad, ctx)).resolves.toBeUndefined();
    expect(r.guards.get('prompt_one')?.()).toBe(true);
    expect(r.presenter.showPromptQuestion).toHaveBeenCalledOnce();
    expect(successes(r.sent)).toHaveLength(1);
  });

  it.each([0, -1, 1.5, NaN, Infinity])('does not stage invalid local generation %s', async (generation) => {
    const r = rig();
    await r.publish(present(), context(generation).value);
    expect(r.presenter.showPromptQuestion).not.toHaveBeenCalled();
    expect(r.sent).toEqual([]);
  });

  it.each(['wrong_prompt', 'wrong_session'] as const)('ignores a cancellation with %s', async (mismatch) => {
    const r = rig();
    const ctx = await r.publish();
    r.controller.observe({ kind: 'prompt.cancel',
      promptId: mismatch === 'wrong_prompt' ? mismatch : 'prompt_one',
      connectionGeneration: mismatch === 'wrong_session' ? mismatch : 'mac_session_one',
    }, ctx);
    expect(r.guards.get('prompt_one')?.()).toBe(true);
    expect(r.state.epoch?.promptId).toBe('prompt_one');
  });
});

describe('PromptedInputController with actual AudioStreamer', () => {
  it.each(['brainstorm', 'terminal', 'messages'] as const)(
    'opens the selected phone microphone from muted for %s and restores mute on cancellation', async (ownerKind) => {
      const r = rig();
      const controls: Array<[boolean, AudioInputSource | undefined]> = [];
      const bridge = { audioControl: async (open: boolean, source?: AudioInputSource) => {
        controls.push([open, source]); return true;
      } };
      const audio = new AudioStreamer({ require: () => bridge, assertStarted: () => bridge } as unknown as BridgeClient,
        r.link as MacLink);
      const controller = new PromptedInputController({ presenter: r.presenter, audio, link: r.link });
      const message = present({ source: 'phone', ownerKind });
      const ctx = context().value;
      try {
        controller.observe(message, ctx);
        await controller.handle(message, ctx);
        expect(audio.isOpen).toBe(true);
        expect(successes(r.sent)).toHaveLength(1);
        expect(controls).toEqual([[true, AudioInputSource.Phone]]);
        controller.cancel('back');
        expect(audio.isOpen).toBe(false);
        await vi.advanceTimersByTimeAsync(0);
        expect(controls[controls.length - 1]?.[0]).toBe(false);
      } finally { controller.cancel('cleanup', false); await audio.stop(); }
    },
  );

  it('cancels a held microphone start without acknowledging or leaving a late hot mic', async () => {
    const r = rig();
    const held = deferred<boolean>();
    const controls: boolean[] = [];
    const bridge = { audioControl: async (open: boolean) => {
      controls.push(open); return open ? held.promise : true;
    } };
    const audio = new AudioStreamer({ require: () => bridge, assertStarted: () => bridge } as unknown as BridgeClient,
      r.link as MacLink);
    const controller = new PromptedInputController({ presenter: r.presenter, audio, link: r.link });
    const message = present(); const ctx = context().value;
    controller.observe(message, ctx);
    const handling = controller.handle(message, ctx);
    await vi.advanceTimersByTimeAsync(0);
    expect(controls).toEqual([true]);
    controller.cancel('back');
    held.resolve(true);
    await handling;
    await vi.advanceTimersByTimeAsync(0);
    expect(successes(r.sent)).toHaveLength(0);
    expect(audio.isOpen).toBe(false);
    expect(controls[controls.length - 1]).toBe(false);
  });

  it('a reentrant cancellation during ack send cannot leave fresh audio armed', async () => {
    const r = rig();
    const sent: UplinkMessage[] = [];
    let controller!: PromptedInputController;
    const link = {
      send: (message: UplinkMessage) => { sent.push(message); },
      sendForGeneration: (message: BoundMessage) => {
        sent.push(message);
        if (message.kind === 'prompt.ack' && message.success) controller.cancel('Back during ack');
        return true;
      },
    };
    const client = { assertStarted: () => ({ audioControl: async () => true }),
      require: () => ({ audioControl: async () => true }) } as unknown as BridgeClient;
    const audio = new AudioStreamer(client, link as unknown as MacLink);
    controller = new PromptedInputController({ presenter: r.presenter, audio, link,
      monotonicMs: () => r.state.now });
    audio.onPromptInvalidated = (id) => controller.audioInvalidated(id);
    const message = present();
    const ctx = context().value;
    await audio.start();
    try {
      controller.observe(message, ctx);
      await controller.handle(message, ctx);
      expect(controller.isActive()).toBe(false);
      audio.ingest([3, 0], 'self', AudioInputSource.Glasses);
      vi.advanceTimersByTime(200);
      expect(sent.map((item) => item.kind)).toEqual(['prompt.ack', 'prompt.cancel', 'audio']);
      expect(sent[2]).not.toHaveProperty('audioEpoch');
    } finally { controller.cancel('cleanup', false); await audio.stop(); }
  });

  it('fails closed when the fallback presenter has no exact-question API', async () => {
    const r = rig();
    const controller = new PromptedInputController({ presenter: {}, audio: r.audio, link: r.link });
    const message = present();
    const ctx = context().value;
    try {
      controller.observe(message, ctx);
      await controller.handle(message, ctx);
      expect(controller.isActive()).toBe(false);
      expect(r.audio.activatePromptEpoch).not.toHaveBeenCalled();
      expect(successes(r.sent)).toEqual([]);
      expect(r.sent.some(({ message }) => message.kind === 'prompt.ack' && !message.success)).toBe(true);
    } finally { controller.cancel('cleanup', false); }
  });

  it('orders old complete PCM, exact ack and fresh epoch PCM without splicing the old odd byte', async () => {
    const r = rig();
    const sent: UplinkMessage[] = [];
    const link = {
      send: (message: UplinkMessage) => { sent.push(message); },
      sendForGeneration: (message: BoundMessage, generation: number) => {
        expect(generation).toBe(7);
        sent.push(message);
        return true;
      },
    };
    const client = { assertStarted: () => ({ audioControl: async () => true }),
      require: () => ({ audioControl: async () => true }) } as unknown as BridgeClient;
    const audio = new AudioStreamer(client, link as unknown as MacLink);
    const controller = new PromptedInputController({ presenter: r.presenter, audio, link,
      monotonicMs: () => r.state.now });
    audio.onPromptInvalidated = (id) => controller.audioInvalidated(id);
    const message = present();
    const ctx = context().value;
    await audio.start();
    try {
      audio.ingest([0x02, 0x01, 0xaa], 'self', AudioInputSource.Glasses);
      controller.observe(message, ctx);
      await controller.handle(message, ctx);
      audio.ingest([3, 0, 4, 0], 'self', AudioInputSource.Glasses);
      vi.advanceTimersByTime(200);
      expect(sent.map((item) => item.kind)).toEqual(['audio', 'prompt.ack', 'audio']);
      expect(sent[0]).toMatchObject({ kind: 'audio', pcm: [258] });
      expect(sent[0]).not.toHaveProperty('audioEpoch');
      expect(sent[2]).toMatchObject({ kind: 'audio', pcm: [3, 4], audioEpoch: message.promptId });
      await audio.stop();
      expect(sent[sent.length - 1]).toEqual({ kind: 'prompt.cancel', promptId: message.promptId,
        connectionGeneration: message.connectionGeneration });
      expect(r.guards.get(message.promptId)?.()).toBe(false);
    } finally { controller.cancel('test cleanup', false); await audio.stop(); }
  });
});
