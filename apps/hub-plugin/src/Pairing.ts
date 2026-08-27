import type { MacLink } from './MacLink';

export const TOKEN_KEY = 'joat.pairing.token';

export function loadToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // storage unavailable: dial bare; the Mac re-pairs if unpaired
  }
}

/**
 * Handle a pairing.set offer from the Mac.
 *
 * The distinctions here were each observed live:
 *
 * - Offer of the SAME token we hold → RE-ACK. The Mac's store is empty (a
 *   lost ack, or an operator recovery) and its window is OPEN to the whole
 *   LAN until an ack lands. The old guard dropped this offer, making
 *   "stored but never acked" an absorbing state: the Mac admitted every
 *   peer for the life of the install and the only exit was wiping the
 *   phone's storage by hand. Re-acking a byte-identical token is free.
 *
 * - Offer of a DIFFERENT token while we hold one → IGNORE. app.json
 *   whitelists plain-ws mDNS names any LAN peer can answer; an unguarded
 *   overwrite lets a rogue "Mac" lock the wearer out of their real brain.
 *
 * - No token held → store, mirror to host storage, ack ONLY after the
 *   write succeeds, so a failed write costs a retry, not a stranding.
 */
export function handlePairingOffer(
  offered: string,
  link: MacLink,
  persistHost: (token: string) => void,
): 'stored' | 'reacked' | 'ignored' | 'session-only' {
  const held = loadToken();
  if (held !== null) {
    if (held === offered) {
      link.setToken(held);
      link.send({ kind: 'pairing', token: held });
      return 'reacked';
    }
    return 'ignored';
  }
  try {
    localStorage.setItem(TOKEN_KEY, offered);
    persistHost(offered);
    link.setToken(offered);
    link.send({ kind: 'pairing', token: offered });
    return 'stored';
  } catch {
    // Storage unavailable (private mode): use it for this session only and
    // stay unpaired, so the next launch can pair for real.
    link.setToken(offered);
    return 'session-only';
  }
}
