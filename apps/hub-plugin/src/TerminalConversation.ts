import type { TerminalConversationMessage } from './types';

interface Row { id: string; line: number; text: string }
interface MessageRows { text: string; role: TerminalConversationMessage['role']; bytes: number; rows: Row[] }
interface ViewState {
  rows: Row[]; top: number; visible: number; follow: boolean;
  layoutKey: string; measured: Map<string, MessageRows>;
  /** A JSON-restored anchor exists before the first post-handoff measurement. */
  anchor?: Pick<Row, 'id' | 'line'>;
}

export interface TerminalConversationViewportSnapshot {
  version: 1;
  conversations: Array<{
    key: string;
    follow: boolean;
    anchor?: { id: string; line: number };
  }>;
}

function safeToken(value: unknown, maximum: number): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum
    && !value.includes('\0');
}

/** Reading position belongs to a conversation, not its transient working state. */
export class TerminalConversationViewport {
  private readonly conversations = new Map<string, ViewState>();
  private active?: ViewState;

  view(key: string, messages: readonly TerminalConversationMessage[], visible: number,
    wrap: (text: string) => string[], historyTruncated = false, layoutKey = 'default'): { lines: string[]; newer: number; older: number } {
    let state = this.conversations.get(key);
    const anchor = state?.rows[state.top] ?? state?.anchor;
    const wasFollowing = state?.follow ?? true;
    const encoder = new TextEncoder();
    const previous = state?.layoutKey === layoutKey ? state.measured : undefined;
    const retained = messages.map(message => {
      const cached = previous?.get(message.id);
      const same = cached?.text === message.text && cached.role === message.role;
      return {message, cached: same ? cached : undefined,
        bytes: same ? cached.bytes : encoder.encode(message.text).length};
    });
    let bytes = retained.reduce((sum, entry) => sum + entry.bytes, 0);
    while (retained.length > 64 || bytes > 262144) {
      const dropped = retained.shift();
      if (!dropped) break;
      bytes -= dropped.bytes;
      historyTruncated = true;
    }
    const measured = new Map<string, MessageRows>();
    const rows: Row[] = [];
    if (historyTruncated) rows.push({id: '__history', line: 0, text: 'Earlier conversation history is no longer available.'});
    for (const {message, cached, bytes: messageBytes} of retained) {
      let entry = cached;
      if (!entry) {
        const prefix = message.role === 'user' ? 'YOU > ' : message.role === 'assistant' ? 'AGENT > ' : '';
        const lines = (prefix + message.text).split(/\r?\n/).flatMap(paragraph => {
          const wrapped = wrap(paragraph);
          return wrapped.length ? wrapped : [''];
        });
        const messageRows = lines.map((text, line) => ({id: message.id, line, text}));
        messageRows.push({id: message.id, line: lines.length, text: ''});
        entry = {text: message.text, role: message.role, bytes: messageBytes, rows: messageRows};
      }
      measured.set(message.id, entry);
      for (const row of entry.rows) rows.push(row);
    }
    if (rows[rows.length - 1]?.text === '') rows.pop();
    const maximum = Math.max(0, rows.length - visible);
    let top = wasFollowing ? maximum : Math.min(state?.top ?? 0, maximum);
    if (!wasFollowing && anchor) {
      const located = rows.findIndex(row => row.id === anchor.id && row.line === anchor.line);
      // If retained history evicts the reading anchor, expose the explicit
      // history notice rather than silently jumping to a newer answer.
      top = located >= 0 ? Math.min(located, maximum) : 0;
    }
    const measuredAnchor = rows[top];
    state = {
      rows, top, visible, follow: wasFollowing, measured, layoutKey,
      ...(measuredAnchor ? { anchor: { id: measuredAnchor.id, line: measuredAnchor.line } }
        : anchor ? { anchor: { ...anchor } } : {}),
    };
    this.conversations.delete(key);
    this.conversations.set(key, state);
    while (this.conversations.size > 8) this.conversations.delete(this.conversations.keys().next().value!);
    this.active = state;
    return {lines: rows.slice(top, top + visible).map(row => row.text), newer: maximum - top, older: top};
  }

  scroll(direction: 'up' | 'down'): boolean {
    const state = this.active;
    if (!state) return false;
    const maximum = Math.max(0, state.rows.length - state.visible);
    const next = direction === 'up' ? Math.max(0, state.top - 6) : Math.min(maximum, state.top + 6);
    if (next === state.top) return false;
    state.top = next;
    state.follow = next === maximum;
    const anchor = state.rows[next];
    state.anchor = anchor ? { id: anchor.id, line: anchor.line } : undefined;
    return true;
  }

  /** Persist semantic reading anchors rather than measured row offsets. The
   * next WebView may wrap text differently, so it re-locates the same message
   * line after applying the current firmware metrics. */
  exportState(): TerminalConversationViewportSnapshot {
    return {
      version: 1,
      conversations: Array.from(this.conversations, ([key, state]) => {
        const row = state.rows[state.top] ?? state.anchor;
        return {
          key,
          follow: state.follow,
          ...(row ? { anchor: { id: row.id, line: row.line } } : {}),
        };
      }),
    };
  }

  /** Adopt only a small, validated reading-position snapshot. Transcript
   * content remains Mac-owned and is re-measured from the next terminal model. */
  importState(raw: unknown): boolean {
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return false;
    const value = raw as Record<string, unknown>;
    if (value.version !== 1 || !Array.isArray(value.conversations)
      || value.conversations.length > 8) return false;
    const restored = new Map<string, ViewState>();
    for (const item of value.conversations) {
      if (typeof item !== 'object' || item === null || Array.isArray(item)) return false;
      const entry = item as Record<string, unknown>;
      if (!safeToken(entry.key, 256) || typeof entry.follow !== 'boolean') return false;
      let anchor: Pick<Row, 'id' | 'line'> | undefined;
      if (entry.anchor !== undefined) {
        if (typeof entry.anchor !== 'object' || entry.anchor === null
          || Array.isArray(entry.anchor)) return false;
        const candidate = entry.anchor as Record<string, unknown>;
        if (!safeToken(candidate.id, 256) || !Number.isSafeInteger(candidate.line)
          || (candidate.line as number) < 0 || (candidate.line as number) > 100_000) return false;
        anchor = { id: candidate.id, line: candidate.line as number };
      }
      if (restored.has(entry.key)) return false;
      restored.set(entry.key, {
        rows: [], top: 0, visible: 1, follow: entry.follow,
        layoutKey: '', measured: new Map(), ...(anchor ? { anchor } : {}),
      });
    }
    this.conversations.clear();
    for (const [key, state] of restored) this.conversations.set(key, state);
    this.active = undefined;
    return true;
  }
}
