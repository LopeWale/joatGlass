/**
 * The phone's IANA timezone — the one fact that makes the Mac's clock and
 * the HUD's clock agree.
 *
 * Every time string the wearer READS is formatted on the Mac, from its own
 * `/etc/localtime`; the banner clock beside it is `new Date()` here, in the
 * phone's WebView. Same desk, they agree. Travel with the glasses and they
 * do not, and nothing in the system notices.
 *
 * The Even Hub SDK cannot help: 0.0.14 exposes audio, IMU, location, photos,
 * device info, user info and local storage, and its `UserInfo` carries
 * `country` — no timezone anywhere. But the WebView is a browser, and every
 * browser knows exactly where it is:
 *
 *     Intl.DateTimeFormat().resolvedOptions().timeZone  // "America/Chicago"
 *
 * That beats deriving a zone from the position the Mac already receives,
 * which needs a tz database and guesses wrong near borders.
 */

/** An IANA name is `Area/Location`, optionally `Area/Region/Location`; a few
 * legacy zones are bare words (`UTC`, `GMT`, `EST5EDT`, `Zulu`). Kept loose
 * on purpose — the Mac validates against real tzdata, which is the only
 * authority that counts. This exists to reject obvious junk before it goes
 * on the wire, not to reimplement the database. */
const IANA_SHAPE = /^[A-Za-z][A-Za-z0-9+_-]*(?:\/[A-Za-z0-9+_-]+){0,2}$/;

/** Matches the Mac's MAX_ZONE_NAME, so a name this side accepts is never
 * one the Mac silently drops. */
const MAX_LENGTH = 64;

/**
 * The phone's zone, or `undefined` when the host cannot say.
 *
 * Never throws. `Intl` is standard in every WebView that can run this
 * plugin, but a resolver that returns an empty string, a bare offset, or
 * nothing at all must degrade to "no opinion" — the Mac then keeps its own
 * zone, which is exactly today's behaviour. A wrong zone is worse than no
 * zone: it would move every event time by a whole number of hours while
 * looking entirely plausible.
 */
export function phoneTimeZone(): string | undefined {
  try {
    const resolved = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (typeof resolved !== 'string') return undefined;
    const name = resolved.trim();
    if (!name || name.length > MAX_LENGTH) return undefined;
    return IANA_SHAPE.test(name) ? name : undefined;
  } catch {
    return undefined;
  }
}
