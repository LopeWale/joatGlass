import type { MacLink, PairingTokensByOrigin } from './MacLink';

/** Legacy, one-brain credential. Kept only so existing installs can migrate. */
export const TOKEN_KEY = 'joat.pairing.token';
/** Durable credential map: one pairing token for each approved Mac brain. */
export const PAIRING_TOKENS_KEY = 'joat.pairing.tokens.v1';
/** The last authenticated approved candidate, not a user-controlled URL. */
export const PREFERRED_BRAIN_KEY = 'joat.brain.preferred-url.v1';
const MAX_STORED_PAIRING_ORIGINS = 16;

function canonicalOrigin(value: string): string | null {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'ws:' || parsed.protocol === 'wss:'
      ? parsed.origin
      : null;
  } catch {
    return null;
  }
}

function isToken(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 4096;
}

/** Parse only a bounded, canonical origin-to-token map from durable storage. */
export function decodePairingTokens(raw: string | null | undefined): PairingTokensByOrigin {
  if (!raw || raw.length > 65_536) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return {};
    const entries = Object.entries(parsed);
    if (entries.length > MAX_STORED_PAIRING_ORIGINS) return {};
    const result: Record<string, string> = {};
    for (const [origin, token] of entries) {
      if (canonicalOrigin(origin) === origin && isToken(token)) result[origin] = token;
    }
    return result;
  } catch {
    return {};
  }
}

/** Serialize a bounded, deterministic map for both local and host storage. */
export function encodePairingTokens(tokens: PairingTokensByOrigin): string {
  const accepted = Object.entries(tokens)
    .filter(([origin, token]) => canonicalOrigin(origin) === origin && isToken(token))
    .sort(([left], [right]) => left.localeCompare(right))
    .slice(0, MAX_STORED_PAIRING_ORIGINS);
  return JSON.stringify(Object.fromEntries(accepted));
}

export function loadPairingTokens(): PairingTokensByOrigin {
  try {
    return decodePairingTokens(localStorage.getItem(PAIRING_TOKENS_KEY));
  } catch {
    return {}; // storage unavailable: pair in-memory; do not broaden trust
  }
}

/** Read the old representation without treating it as a universal token. */
export function loadToken(): string | null {
  try {
    const token = localStorage.getItem(TOKEN_KEY);
    return isToken(token) ? token : null;
  } catch {
    return null;
  }
}

/** Host storage is a mirror, not a second trust authority. */
export function mergePairingTokens(
  primary: PairingTokensByOrigin,
  fallback: PairingTokensByOrigin,
): PairingTokensByOrigin {
  return { ...fallback, ...primary };
}

/** Convert a legacy token only to the first configured brain; never spray it. */
export function migrateLegacyToken(
  tokens: PairingTokensByOrigin,
  legacy: string | null | undefined,
  firstCandidate: string | undefined,
): PairingTokensByOrigin {
  if (Object.keys(tokens).length > 0 || !isToken(legacy) || !firstCandidate) return tokens;
  const origin = canonicalOrigin(firstCandidate);
  return origin ? { [origin]: legacy } : tokens;
}

function persistPairingTokens(tokens: PairingTokensByOrigin): void {
  localStorage.setItem(PAIRING_TOKENS_KEY, encodePairingTokens(tokens));
}

/**
 * Handle a pairing.set offer from the Mac.
 *
 * The distinctions here were each observed live:
 *
 * - Offer of the SAME token at the CURRENT Mac origin → RE-ACK. The Mac's store is empty (a
 *   lost ack, or an operator recovery) and its window is OPEN to the whole
 *   LAN until an ack lands. The old guard dropped this offer, making
 *   "stored but never acked" an absorbing state: the Mac admitted every
 *   peer for the life of the install and the only exit was wiping the
 *   phone's storage by hand. Re-acking a byte-identical token is free.
 *
 * - Offer of a DIFFERENT token at the SAME origin while we hold one → IGNORE. app.json
 *   whitelists plain-ws mDNS names any LAN peer can answer; an unguarded
 *   overwrite lets a rogue "Mac" lock the wearer out of their real brain.
 *
 * - No token held for the current origin → store, mirror the full map to host
 *   storage, ack ONLY after the
 *   write succeeds, so a failed write costs a retry, not a stranding.
 */
export function handlePairingOffer(
  offered: string,
  link: MacLink,
  persistHost: (tokens: string) => void,
): 'stored' | 'reacked' | 'ignored' | 'storage-unavailable' {
  const held = link.pairingToken;
  if (held !== null) {
    if (held === offered) {
      link.setToken(held);
      link.send({ kind: 'pairing', token: held });
      return 'reacked';
    }
    return 'ignored';
  }
  try {
    link.setToken(offered);
    const encoded = encodePairingTokens(link.pairingTokens);
    persistPairingTokens(link.pairingTokens);
    persistHost(encoded);
    link.send({ kind: 'pairing', token: offered });
    return 'stored';
  } catch {
    // Pairing acknowledgement grants the Mac a durable credential. If this
    // page cannot persist it, do not retain an in-memory lookalike: a repeat
    // offer must still retry storage rather than accidentally acknowledging a
    // token that vanishes at the next launch.
    link.clearToken();
    return 'storage-unavailable';
  }
}
