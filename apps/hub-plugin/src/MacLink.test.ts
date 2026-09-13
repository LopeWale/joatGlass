import { afterEach, describe, expect, it, vi } from 'vitest';
import { MacLink, type DownlinkContext } from './MacLink';
import type { UplinkMessage } from './types';

/**
 * Stub WebSocket capturing every constructor URL — vitest runs in node.
 * Dial-time behaviour is all these tests need; no frames ever flow.
 */
const dialed: string[] = [];

const sockets: FakeWebSocket[] = [];

class FakeWebSocket {
  static readonly OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  sent: string[] = [];
  constructor(url: string) {
    dialed.push(url);
    sockets.push(this);
  }
  send(data: string): void { this.sent.push(data); }
  close(code = 1006): void {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.({ code } as never);
  }
  /** Simulate the server accepting this dial. */
  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  receive(message: unknown): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
}

(globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket;

describe('MacLink prompt ingress interruption', () => {
  it('revokes a question immediately while keeping ordinary downlinks ordered', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], token: 'fixture' });
    let release!: () => void;
    const painting = new Promise<void>((resolve) => { release = resolve; });
    const order: string[] = [];
    let armed = true;
    link.onMessageIngress((message) => {
      order.push(`ingress:${message.kind}`);
      if (message.kind === 'prompt.cancel') armed = false;
    });
    link.onMessage(async (message) => {
      order.push(`start:${message.kind}`);
      if (message.kind === 'prompt.present') {
        await painting;
        expect(armed).toBe(false);
      }
      order.push(`end:${message.kind}`);
    });
    link.connect();
    const socket = sockets[sockets.length - 1]!;
    socket.open();
    socket.receive({ kind: 'auth.accepted' });
    socket.receive({ kind: 'prompt.present' });
    await Promise.resolve();
    socket.receive({ kind: 'prompt.cancel' });
    expect(armed).toBe(false);
    expect(order).toEqual([
      'ingress:prompt.present', 'start:prompt.present', 'ingress:prompt.cancel',
    ]);
    release();
    await vi.waitFor(() => expect(order).toContain('end:prompt.cancel'));
    expect(order.slice(-3)).toEqual([
      'end:prompt.present', 'start:prompt.cancel', 'end:prompt.cancel',
    ]);
    link.close();
  });

  it('never observes unauthenticated frames, pairing offers or old socket callbacks', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], token: 'fixture' });
    const ingress = vi.fn();
    link.onMessageIngress(ingress);
    link.connect();
    const first = sockets[sockets.length - 1]!;
    first.open();
    first.receive({ kind: 'pairing.set', token: 'candidate' });
    first.receive({ kind: 'prompt.cancel' });
    expect(ingress).not.toHaveBeenCalled();
    first.receive({ kind: 'auth.accepted' });
    const oldCallback = first.onmessage!;
    first.receive({ kind: 'prompt.cancel' });
    expect(ingress).toHaveBeenCalledTimes(1);
    const context = ingress.mock.calls[0]![1] as DownlinkContext;
    link.connect();
    expect(context.signal.aborted).toBe(true);
    oldCallback({ data: JSON.stringify({ kind: 'prompt.present' }) });
    expect(ingress).toHaveBeenCalledTimes(1);
    link.close();
  });

  it('contains an observer failure and continues normal ordered delivery', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], token: 'fixture' });
    const handled = vi.fn();
    link.onMessageIngress(() => { throw new Error('private question fixture'); });
    link.onMessage(handled);
    link.connect();
    const socket = sockets[sockets.length - 1]!;
    socket.open();
    socket.receive({ kind: 'auth.accepted' });
    socket.receive({ kind: 'hud.text', text: 'normal fixture', containerId: 4 });
    await vi.waitFor(() => expect(handled).toHaveBeenCalledTimes(1));
    link.close();
  });
});

afterEach(() => {
  dialed.length = 0;
  sockets.length = 0;
  vi.useRealTimers();
});

describe('MacLink pairing token', () => {
  it('dials bare when unpaired — auth belongs in the first frame', () => {
    new MacLink({ urls: ['ws://mac.local:8787/glasses'] }).connect();
    expect(dialed).toEqual(['ws://mac.local:8787/glasses']);
  });

  it('never puts a credential in the WebSocket URL', () => {
    new MacLink({ urls: ['ws://mac.local:8787/glasses'], token: 'abc123' }).connect();
    expect(dialed).toEqual(['ws://mac.local:8787/glasses']);
  });

  it('sends the held credential as the first application frame', () => {
    new MacLink({ urls: ['ws://m:1/g'], token: 'a/b+c' }).connect();
    sockets[0]!.open();
    expect(sockets[0]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: 'a/b+c' })]);
  });

  it('sends an explicit null credential when unpaired', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    sockets[0]!.open();
    expect(sockets[0]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: null })]);
  });

  it('does not report connected or flush uplinks before auth is accepted', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], token: 't' });
    link.send({ kind: 'policy', resume: 'auto' });
    link.connect();
    sockets[0]!.open();
    expect(link.linkState).toBe('connecting');
    expect(sockets[0]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: 't' })]);
    sockets[0]!.receive({ kind: 'auth.accepted' });
    expect(link.linkState).toBe('connected');
    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: 't' }),
      JSON.stringify({ kind: 'policy', resume: 'auto' }),
    ]);
  });

  it('keeps activeUrl clean for status surfaces — no token leak to the panel', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], token: 'secret' });
    link.connect();
    expect(link.activeUrl).toBe('ws://m:1/g');
  });

  it('uses the pairing token belonging to each approved brain, never a global token', () => {
    vi.useFakeTimers();
    const link = new MacLink({
      urls: ['ws://a:1/g', 'ws://b:1/g'],
      tokens: { 'ws://a:1': 'token-a', 'ws://b:1': 'token-b' },
      reconnectBaseMs: 10,
      reconnectMaxMs: 10,
    });
    link.connect();
    sockets[0]!.open();
    expect(sockets[0]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: 'token-a' })]);

    sockets[0]!.close();
    vi.advanceTimersByTime(20);
    sockets[1]!.open();
    expect(sockets[1]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: 'token-b' })]);
  });

  it('starts at a stored healthy approved brain rather than always candidate zero', () => {
    const link = new MacLink({
      urls: ['wss://a:443/glasses', 'wss://b:443/glasses'],
      preferredUrl: 'wss://b:443/glasses',
      tokens: { 'wss://b': 'token-b' },
    });
    link.connect();

    expect(dialed).toEqual(['wss://b:443/glasses']);
    sockets[0]!.open();
    expect(sockets[0]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: 'token-b' })]);
  });

  it('forgets only the Mac that explicitly rejects its own token', () => {
    vi.useFakeTimers();
    const link = new MacLink({
      urls: ['ws://a:1/g', 'ws://b:1/g'],
      tokens: { 'ws://a:1': 'token-a', 'ws://b:1': 'token-b' },
      reconnectBaseMs: 10,
      reconnectMaxMs: 10,
    });
    const stale: Array<{ origin: string; tokens: Record<string, string> }> = [];
    link.onStaleToken((origin, tokens) => stale.push({ origin, tokens: { ...tokens } }));

    link.connect();
    sockets[0]!.open();
    sockets[0]!.close(4401); // first explicit rejection at A
    vi.advanceTimersByTime(30);

    sockets[1]!.open();
    sockets[1]!.receive({ kind: 'auth.accepted' }); // B remains independently healthy
    sockets[1]!.close(1006);
    vi.advanceTimersByTime(30);

    sockets[2]!.open();
    sockets[2]!.close(4401); // second complete rotation reaches A again

    expect(link.pairingTokens).toEqual({ 'ws://b:1': 'token-b' });
    expect(stale).toEqual([{
      origin: 'ws://a:1',
      tokens: { 'ws://b:1': 'token-b' },
    }]);
  });
});

describe('MacLink generation-bound delivery', () => {
  type BoundMessage = Extract<UplinkMessage, { kind: 'audio' | 'prompt.ack' | 'prompt.cancel' }>;
  const messages: BoundMessage[] = [
    { kind: 'audio', source: 'glasses', seq: 1, pcm: [258], audioEpoch: 'prompt-1' },
    {
      kind: 'prompt.ack', promptId: 'prompt-1', presentationBinding: 'binding-1',
      connectionGeneration: 'mac-audio-session', source: 'glasses', success: true,
      audioEpoch: 'prompt-1',
    },
    { kind: 'prompt.cancel', promptId: 'prompt-1', connectionGeneration: 'mac-audio-session' },
  ];
  const cases = messages.map((message) => [message.kind, message] as const);

  function send(link: MacLink, message: BoundMessage, generation: number): boolean {
    return link.sendForGeneration(message, generation);
  }
  async function authenticate(link: MacLink): Promise<{
    socket: FakeWebSocket; context: DownlinkContext;
  }> {
    let context: DownlinkContext | undefined;
    link.onMessage((_message, received) => { context = received; });
    link.connect();
    const socket = sockets[sockets.length - 1]!;
    socket.open();
    socket.receive({ kind: 'auth.accepted' });
    socket.receive({ kind: 'hud.text', containerId: 4, text: 'context fixture' });
    await Promise.resolve();
    expect(context).toBeDefined();
    return { socket, context: context! };
  }

  it.each(cases)('drops offline %s without replaying it or evicting wearer input', async (_kind, message) => {
    const link = new MacLink({ urls: ['ws://m:1/g'], maxQueue: 1 });
    try {
      link.send({ kind: 'input', event: { eventType: 0 } });
      const accepted = send(link, message, 1);
      const { socket } = await authenticate(link);
      expect(socket.sent.map((value) => JSON.parse(value))).toEqual([
        { kind: 'auth', token: null }, { kind: 'input', event: { eventType: 0 } },
      ]);
      expect(accepted).toBe(false);
    } finally {
      link.close();
    }
  });

  it.each(cases)('never queues %s on an open but unauthenticated socket', (_kind, message) => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    try {
      link.connect();
      const socket = sockets[sockets.length - 1]!;
      socket.open();
      const accepted = send(link, message, 1);
      expect(socket.sent.map((value) => JSON.parse(value))).toEqual([{ kind: 'auth', token: null }]);
      socket.receive({ kind: 'auth.accepted' });
      expect(socket.sent.map((value) => JSON.parse(value))).toEqual([{ kind: 'auth', token: null }]);
      expect(accepted).toBe(false);
    } finally {
      link.close();
    }
  });

  it.each(cases)('sends %s only through its current authenticated generation', async (_kind, message) => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    try {
      const { socket, context } = await authenticate(link);
      expect(context.signal.aborted).toBe(false);
      expect(send(link, message, context.generation)).toBe(true);
      expect(socket.sent.map((value) => JSON.parse(value))).toEqual([
        { kind: 'auth', token: null }, message,
      ]);
    } finally {
      link.close();
    }
  });

  it.each(cases)('rejects a mismatched generation for %s without later replay', async (_kind, message) => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    try {
      const { socket, context } = await authenticate(link);
      const accepted = send(link, message, context.generation + 1);
      const replacement = await authenticate(link);
      expect(socket.sent.map((value) => JSON.parse(value))).toEqual([{ kind: 'auth', token: null }]);
      expect(replacement.socket.sent.map((value) => JSON.parse(value))).toEqual([{ kind: 'auth', token: null }]);
      expect(accepted).toBe(false);
    } finally {
      link.close();
    }
  });

  it.each(cases)('never sends an abandoned generation\'s %s to its replacement', async (_kind, message) => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    try {
      const original = await authenticate(link);
      const replacement = await authenticate(link);
      expect(original.context.signal.aborted).toBe(true);
      const accepted = send(link, message, original.context.generation);
      expect(replacement.socket.sent.map((value) => JSON.parse(value))).toEqual([{ kind: 'auth', token: null }]);
      expect(accepted).toBe(false);
    } finally {
      link.close();
    }
  });

  it.each(cases)('contains a socket send failure for %s without queueing a retry', async (_kind, message) => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const original = await authenticate(link);
    const failing = vi.spyOn(original.socket, 'send').mockImplementation(() => {
      throw new Error('controlled socket failure');
    });
    try {
      let accepted: boolean | undefined;
      expect(() => { accepted = send(link, message, original.context.generation); }).not.toThrow();
      expect(accepted).toBe(false);
      failing.mockRestore();
      const replacement = await authenticate(link);
      expect(replacement.socket.sent.map((value) => JSON.parse(value))).toEqual([{ kind: 'auth', token: null }]);
    } finally {
      failing.mockRestore();
      link.close();
    }
  });

  it('rejects an aborted dispatcher even before its open socket finishes closing', async () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://m:1/g'], downlinkHandlerTimeoutMs: 10 });
    const { socket, context } = await authenticate(link);
    let release!: () => void;
    let entered = false;
    const hold = new Promise<void>((resolve) => { release = resolve; });
    link.onMessage(async () => { entered = true; await hold; });
    const delayedClose = vi.spyOn(socket, 'close').mockImplementation(() => {});
    try {
      socket.receive({ kind: 'hud.text', containerId: 4, text: 'held fixture' });
      await vi.waitFor(() => expect(entered).toBe(true), { timeout: 100, interval: 1 });
      vi.advanceTimersByTime(10);
      expect(context.signal.aborted).toBe(true);
      expect(socket.readyState).toBe(FakeWebSocket.OPEN);

      const accepted = send(link, messages[0]!, context.generation);
      expect(socket.sent.map((value) => JSON.parse(value))).toEqual([{ kind: 'auth', token: null }]);
      expect(accepted).toBe(false);
    } finally {
      release();
      await hold;
      delayedClose.mockRestore();
      link.close();
    }
  });
});

describe('MacLink diagnostic delivery', () => {
  it('drops offline diagnostic bursts without evicting queued wearer input', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.send({ kind: 'input', event: { eventType: 0 } });
    for (let frame = 1; frame <= 400; frame += 1) {
      link.sendDiagnostic({
        kind: 'g2.diagnostic',
        event: {
          type: 'tile_upload', frame, order: 0, containerId: 4, outcome: 'success',
          durationMs: 900, bytes: 4096,
        },
      });
    }

    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
      JSON.stringify({ kind: 'input', event: { eventType: 0 } }),
    ]);
  });

  it('sends the latest pending phone fix ahead of offline audio after authentication', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], maxQueue: 2 });
    link.send({ kind: 'audio', source: 'glasses', seq: 1, pcm: [1] });
    link.sendLatestLocation({
      kind: 'location', latitude: 41.9, longitude: -87.65, timestamp: 1_725_000_000_123,
    });
    link.send({ kind: 'audio', source: 'glasses', seq: 2, pcm: [2] });
    link.send({ kind: 'audio', source: 'glasses', seq: 3, pcm: [3] });

    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
      JSON.stringify({
        kind: 'location', latitude: 41.9, longitude: -87.65, timestamp: 1_725_000_000_123,
      }),
      JSON.stringify({ kind: 'audio', source: 'glasses', seq: 2, pcm: [2] }),
      JSON.stringify({ kind: 'audio', source: 'glasses', seq: 3, pcm: [3] }),
    ]);
  });
});

describe('MacLink navigation delivery', () => {
  it('drops session-bound navigation uplinks instead of crossing reconnects', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.sendNavigation({
      kind: 'navigation.origin', sessionId: 'nav-old', requestId: 'origin-old',
      generation: 1, latitude: 41.9, longitude: -87.65,
    });

    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
    ]);
  });

  it('sends session-bound navigation uplinks only on the authenticated socket', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    link.sendNavigation({
      kind: 'navigation.view.ack', sessionId: 'nav-1', requestId: 'mode-1',
      generation: 1, outcome: 'bridge_acknowledged',
    });

    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
      JSON.stringify({
        kind: 'navigation.view.ack', sessionId: 'nav-1', requestId: 'mode-1',
        generation: 1, outcome: 'bridge_acknowledged',
      }),
    ]);
  });
});

describe('MacLink assistant conversation action delivery', () => {
  const action = {
    kind: 'input' as const,
    event: {
      containerName: 'assistant-conversation',
      selectedName: 'exit',
      conversationId: 'voice-old',
      conversationRevision: 9,
    },
  };

  it('drops an offline conversation action instead of replaying it after reconnect', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    expect(link.sendConversationAction(action)).toBe(false);

    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
    ]);
  });

  it('sends an exact conversation action only on the authenticated socket', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    expect(link.sendConversationAction(action)).toBe(true);
    expect(sockets[0]!.sent).toContain(JSON.stringify(action));
  });
});

describe('MacLink downlink ordering', () => {
  it('coalesces adjacent queued assistant appends before the HUD renders them', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const handled: Array<{ operation: string; text?: string }> = [];
    let releaseReplace!: () => void;
    const replaceBlocked = new Promise<void>((resolve) => { releaseReplace = resolve; });
    link.onMessage(async (message) => {
      if (message.kind !== 'assistant.overlay') return;
      handled.push({ operation: message.operation, text: message.text });
      if (message.operation === 'replace') await replaceBlocked;
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    sockets[0]!.receive({ kind: 'assistant.overlay', operation: 'replace', text: 'Hello' });
    await Promise.resolve();
    expect(handled).toEqual([{ operation: 'replace', text: 'Hello' }]);

    sockets[0]!.receive({ kind: 'assistant.overlay', operation: 'append', text: ' one' });
    sockets[0]!.receive({ kind: 'assistant.overlay', operation: 'append', text: ' two' });
    sockets[0]!.receive({ kind: 'assistant.overlay', operation: 'append', text: ' three' });
    sockets[0]!.receive({
      kind: 'assistant.overlay', operation: 'settle', dismissAfterMs: 6_000,
    });

    releaseReplace();
    await replaceBlocked;
    await vi.waitFor(() => {
      expect(handled).toEqual([
        { operation: 'replace', text: 'Hello' },
        { operation: 'append', text: ' one two three' },
        { operation: 'settle', text: undefined },
      ]);
    });
  });

  it('awaits each async HUD handler before dispatching the next frame', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const order: string[] = [];
    let releaseRebuild!: () => void;
    const rebuildBlocked = new Promise<void>((resolve) => { releaseRebuild = resolve; });
    link.onMessage(async (message) => {
      order.push(`start:${message.kind}`);
      if (message.kind === 'hud.rebuild') await rebuildBlocked;
      order.push(`end:${message.kind}`);
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    sockets[0]!.receive({ kind: 'hud.rebuild', reason: 'brainstorm' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'first turn' });
    await Promise.resolve();
    expect(order).toEqual(['start:hud.rebuild']);

    releaseRebuild();
    await rebuildBlocked;
    await vi.waitFor(() => {
      expect(order).toEqual([
        'start:hud.rebuild',
        'end:hud.rebuild',
        'start:hud.text',
        'end:hud.text',
      ]);
    });
  });

  it('contains a rejected handler and continues with later frames', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const handled: string[] = [];
    link.onMessage(async (message) => {
      if (message.kind === 'hud.rebuild') throw new Error('render failed');
      handled.push(message.kind);
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    sockets[0]!.receive({ kind: 'hud.rebuild', reason: 'brainstorm' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'still handled' });
    await vi.waitFor(() => expect(handled).toEqual(['hud.text']));
  });

  it('a hung old connection does not wedge downlinks after reconnect', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const handled: string[] = [];
    const neverFinishes = new Promise<void>(() => {});
    link.onMessage(async (message) => {
      if (message.kind !== 'hud.text') return;
      if (message.text === 'old') await neverFinishes;
      handled.push(message.text);
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'old' });
    await Promise.resolve();

    link.connect();
    sockets[1]!.open();
    sockets[1]!.receive({ kind: 'auth.accepted' });
    sockets[1]!.receive({ kind: 'hud.text', containerId: 4, text: 'new' });

    await vi.waitFor(() => expect(handled).toEqual(['new']));
  });

  it('aborts an in-flight generation before it can commit after reconnect', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const commits: string[] = [];
    const contexts: Array<{ generation: number; signal: AbortSignal }> = [];
    let releaseOld!: () => void;
    const oldBlocked = new Promise<void>((resolve) => { releaseOld = resolve; });
    link.onMessage(async (message, context) => {
      contexts.push(context);
      if (message.kind === 'hud.text' && message.text === 'old') await oldBlocked;
      context.signal.throwIfAborted();
      commits.push(message.kind === 'hud.text' ? message.text : message.kind);
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'old' });
    await Promise.resolve();

    link.connect();
    sockets[1]!.open();
    sockets[1]!.receive({ kind: 'auth.accepted' });
    sockets[1]!.receive({ kind: 'hud.text', containerId: 4, text: 'new' });
    await vi.waitFor(() => expect(commits).toEqual(['new']));

    releaseOld();
    await oldBlocked;
    await Promise.resolve();
    expect(commits).toEqual(['new']);
    expect(contexts).toHaveLength(2);
    expect(contexts[0]!.signal.aborted).toBe(true);
    expect(contexts[1]!.signal.aborted).toBe(false);
    expect(contexts[1]!.generation).toBeGreaterThan(contexts[0]!.generation);
  });

  it('never starts queued work from an abandoned generation', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const started: string[] = [];
    let releaseOld!: () => void;
    const oldBlocked = new Promise<void>((resolve) => { releaseOld = resolve; });
    link.onMessage(async (message) => {
      if (message.kind !== 'hud.text') return;
      started.push(message.text);
      if (message.text === 'old-running') await oldBlocked;
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'old-running' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'old-queued' });
    await Promise.resolve();

    link.connect();
    sockets[1]!.open();
    sockets[1]!.receive({ kind: 'auth.accepted' });
    sockets[1]!.receive({ kind: 'hud.text', containerId: 4, text: 'new' });
    await vi.waitFor(() => expect(started).toEqual(['old-running', 'new']));

    releaseOld();
    await oldBlocked;
    await Promise.resolve();
    await Promise.resolve();
    expect(started).toEqual(['old-running', 'new']);
  });

  it('expires a hung handler by aborting its generation and reconnecting', async () => {
    vi.useFakeTimers();
    const link = new MacLink({
      urls: ['ws://m:1/g'],
      reconnectBaseMs: 10,
      reconnectMaxMs: 10,
      downlinkHandlerTimeoutMs: 50,
    });
    let signal: AbortSignal | undefined;
    link.onMessage(async (_message, context) => {
      signal = context.signal;
      await new Promise<void>(() => {});
    });
    link.connect();
    const first = sockets[0]!;
    first.open();
    first.receive({ kind: 'auth.accepted' });
    first.receive({ kind: 'hud.text', containerId: 4, text: 'wedged' });
    await Promise.resolve();

    vi.advanceTimersByTime(50);
    expect(signal?.aborted).toBe(true);
    expect(first.closed).toBe(true);

    vi.advanceTimersByTime(20);
    expect(sockets).toHaveLength(2);
  });
});


describe('MacLink reconnect storm', () => {
  /**
   * Observed on real G2 hardware: ~2 reconnects/second, indefinitely. A
   * superseded socket kept its handlers, so its close scheduled ANOTHER
   * dial that raced the current one; both opened, the newest-wins server
   * dropped the older, and its close dialed again. The loop compounds.
   *
   * Retrying forever while down is CORRECT, so these assert the invariant
   * that actually broke: one drop must produce exactly one dial.
   */
  const SETTLE_MS = 1_000; // base backoff 500ms + up to 20% jitter

  it('retains a fast connection state for a HUD observer created later', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    expect(link.linkState).toBe('connected');
  });

  it('abandons the previous socket when dialing again', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    const first = sockets[0]!;
    link.connect();
    expect(first.closed).toBe(true);
    expect(first.onclose).toBeNull(); // detached: cannot schedule a dial
  });

  it('a superseded socket closing does NOT schedule a reconnect', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    const stale = sockets[0]!;
    link.connect();
    const before = dialed.length;
    stale.onclose?.({ code: 1006 }); // the server dropped the older socket, late
    vi.advanceTimersByTime(SETTLE_MS);
    expect(dialed.length).toBe(before); // no dial from a socket we replaced
  });

  it('coalesces repeated closes of one socket into a single dial', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    const before = dialed.length;
    const live = sockets[0]!;
    live.close();
    live.onclose?.({ code: 1006 }); // spurious repeats must not compound
    live.onclose?.({ code: 1006 });
    vi.advanceTimersByTime(SETTLE_MS);
    expect(dialed.length).toBe(before + 1);
  });

  it('an opened socket still reconnects exactly once when it drops', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    const before = dialed.length;
    sockets[0]!.open();
    sockets[0]!.close();
    vi.advanceTimersByTime(SETTLE_MS);
    expect(dialed.length).toBe(before + 1);
  });

  it('the dial watchdog retries a hung dial, but only one at a time', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://m:1/g'], dialTimeoutMs: 100 });
    link.connect();
    const before = dialed.length;
    // Socket never opens: watchdog closes it, which schedules one retry.
    vi.advanceTimersByTime(100 + SETTLE_MS);
    expect(dialed.length).toBe(before + 1);
  });

  it.each(['dial', 'authentication'] as const)(
    'retries a timed-out %s without waiting for the browser close event',
    (phase) => {
      vi.useFakeTimers();
      const link = new MacLink({
        urls: ['ws://a:1/g', 'ws://b:1/g'], dialTimeoutMs: 100,
        reconnectBaseMs: 10, reconnectMaxMs: 10,
      });
      link.connect();
      const stalled = sockets[0]!;
      if (phase === 'authentication') stalled.open();
      const lateClose = stalled.onclose;
      // Browser close() starts asynchronous teardown; its close event need
      // not arrive within our application deadline.
      stalled.close = () => { stalled.readyState = 2; };
      vi.advanceTimersByTime(120);
      expect(dialed).toEqual(['ws://a:1/g', 'ws://b:1/g']);
      const replacement = sockets[1]!;
      replacement.open();
      replacement.receive({ kind: 'auth.accepted' });
      lateClose?.({ code: 1006 });
      vi.advanceTimersByTime(SETTLE_MS);
      expect(link.linkState).toBe('connected');
      expect(dialed).toHaveLength(2);
      link.close();
    },
  );

  it.each(['manual reconnect', 'close and reopen'] as const)(
    'does not let a pending retry replace a successful %s',
    (action) => {
      vi.useFakeTimers();
      const link = new MacLink({ urls: ['ws://m:1/g'] });
      link.connect();
      sockets[0]!.close();
      if (action === 'close and reopen') link.close();
      link.connect();
      const recovered = sockets[1]!;
      recovered.open();
      recovered.receive({ kind: 'auth.accepted' });
      vi.advanceTimersByTime(SETTLE_MS);
      expect(dialed).toHaveLength(2);
      expect(recovered.closed).toBe(false);
      expect(link.linkState).toBe('connected');
      link.close();
    },
  );

  it.each(['close event', 'deadline'] as const)(
    'honors a reconnect made by the state observer during a %s',
    (trigger) => {
      vi.useFakeTimers();
      const link = new MacLink({ urls: ['ws://m:1/g'], dialTimeoutMs: 100 });
      let recovered = false;
      link.onState((state) => {
        if (state !== 'reconnecting' || recovered) return;
        recovered = true;
        link.connect();
        sockets[1]!.open();
        sockets[1]!.receive({ kind: 'auth.accepted' });
      });
      link.connect();
      if (trigger === 'deadline') vi.advanceTimersByTime(100);
      else sockets[0]!.close();
      vi.advanceTimersByTime(SETTLE_MS);
      expect(dialed).toHaveLength(2);
      expect(link.linkState).toBe('connected');
      link.close();
    },
  );
});

describe('MacLink replaced display', () => {
  it('takes over once when the host restores a retired foreground WebView', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://a:1/g'], token: 'paired' });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    sockets[0]!.close(4409);
    link.resumeIfReplaced();
    link.resumeIfReplaced(); // restore-state and foreground-enter can both fire
    expect(dialed).toHaveLength(2);
    sockets[1]!.open();
    sockets[1]!.receive({ kind: 'auth.accepted' });
    link.resumeIfReplaced();
    vi.advanceTimersByTime(60_000);
    expect(dialed).toHaveLength(2);
    expect(link.linkState).toBe('connected');
    link.close();
    link.resumeIfReplaced(); // explicit teardown is terminal
    expect(dialed).toHaveLength(2);
  });

  it('yields after authenticated replacement until an explicit reconnect', async () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://a:1/g', 'ws://b:1/g'], token: 'paired' });
    let context: DownlinkContext | undefined;
    link.onMessageIngress((_message, owned) => { context = owned; });
    link.connect();
    const first = sockets[0]!;
    first.open();
    first.receive({ kind: 'auth.accepted' });
    first.receive({ kind: 'hud.text', text: 'old display' });
    first.close(4409);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(dialed).toEqual(['ws://a:1/g']);
    expect(link.linkState).toBe('closed');
    expect(link.wasReplaced).toBe(true);
    expect(context?.signal.aborted).toBe(true);
    expect(link.pairingToken).toBe('paired');

    // Input produced during retirement cannot appear on a later owner.
    link.send({ kind: 'hello', model: 'retired', sn: 'fixture', launchSource: 'unknown' });
    link.connect();
    expect(link.wasReplaced).toBe(false);
    expect(dialed).toEqual(['ws://a:1/g', 'ws://a:1/g']);
    sockets[1]!.open();
    sockets[1]!.receive({ kind: 'auth.accepted' });
    expect(sockets[1]!.sent.map((value) => JSON.parse(value))).toEqual([{ kind: 'auth', token: 'paired' }]);
    expect(link.linkState).toBe('connected');
    link.close();
  });

  it.each([1000, 1006])('still retries ordinary authenticated closure %s', (code) => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://a:1/g'], token: 'paired' });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    sockets[0]!.close(code);
    vi.advanceTimersByTime(700);
    expect(dialed).toHaveLength(2);
    expect(link.pairingToken).toBe('paired');
    link.close();
  });

  it('does not accept a replacement claim before authentication', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://a:1/g'], token: 'paired' });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.close(4409);
    vi.advanceTimersByTime(700);
    expect(dialed).toHaveLength(2);
    expect(link.pairingToken).toBe('paired');
    link.close();
  });
});

describe('MacLink stale pairing token', () => {
  /**
   * Application auth closes with 4401 after the WebSocket opens. Generic
   * network failures must never be mistaken for stale credentials.
   */
  // Long enough for one backoff (500ms + jitter), short enough that the 4s
  // dial watchdog does not fire and count a failure of its own.
  const RETRY_MS = 1_000;

  it('drops a token that keeps failing, and reports it once', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://a:1/g', 'ws://b:1/g'], token: 'stale' });
    let forgotten = 0;
    link.onStaleToken(() => { forgotten += 1; });
    link.connect();
    for (let i = 0; i < 5; i += 1) {
      sockets[sockets.length - 1]!.open();
      sockets[sockets.length - 1]!.close(4401);
      vi.advanceTimersByTime(RETRY_MS);
    }
    expect(forgotten).toBe(1);
    expect(dialed[dialed.length - 1]).not.toContain('token=');
  });

  it('a successful connect resets the failure count', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://a:1/g'], token: 't' });
    let forgotten = 0;
    link.onStaleToken(() => { forgotten += 1; });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.close(4401);               // one explicit auth failure
    vi.advanceTimersByTime(RETRY_MS);
    sockets[sockets.length - 1]!.open();
    sockets[sockets.length - 1]!.receive({ kind: 'auth.accepted' }); // resets
    sockets[sockets.length - 1]!.close();   // and dropped again later
    vi.advanceTimersByTime(RETRY_MS);
    expect(forgotten).toBe(0); // intermittency is not staleness
  });
});


describe('explicit Mac choice survives connection failures', () => {
  it.each(['dial', 'authentication', 'disconnect'] as const)('does not visit a healthy other Mac after %s failure', (failure) => {
    vi.useFakeTimers();
    const mini = 'ws://mac-mini:19080/glasses';
    const book = 'ws://macbook:19081/glasses';
    const link = new MacLink({ urls: [mini, book], dialTimeoutMs: 40,
      reconnectBaseMs: 10, reconnectMaxMs: 10 });
    expect(link.selectUrl(book)).toBe(true);
    for (let i = 0; i < 4; i++) {
      const socket = sockets[sockets.length - 1]!;
      if (failure === 'dial') vi.advanceTimersByTime(40);
      else {
        socket.open();
        if (failure === 'disconnect') socket.receive({ kind: 'auth.accepted' });
        socket.close(failure === 'authentication' ? 4401 : 1006);
      }
      vi.advanceTimersByTime(12);
    }
    expect(new Set(dialed)).toEqual(new Set([book]));
    link.close();
  });

  it('restores a selected Mac that has not authenticated yet and allows an explicit change', () => {
    vi.useFakeTimers();
    const mini = 'ws://mac-mini:19080/glasses';
    const book = 'ws://macbook:19081/glasses';
    const link = new MacLink({ urls: [mini, book], preferredUrl: book,
      dialTimeoutMs: 40, reconnectBaseMs: 10, reconnectMaxMs: 10 });
    link.connect();
    vi.advanceTimersByTime(52);
    expect(dialed).toEqual([book, book]);
    expect(link.selectUrl('ws://unapproved:19081/glasses')).toBe(false);
    expect(link.activeUrl).toBe(book);
    expect(link.selectUrl(mini)).toBe(true);
    vi.advanceTimersByTime(52);
    expect(dialed.slice(-2)).toEqual([mini, mini]);
    link.close();
  });
});
