import type { ResumePolicy } from './types';

const STORAGE_KEY = 'joat.settings.v1';

const RESUME_POLICIES: readonly ResumePolicy[] = ['auto', 'mac', 'confirm'];

function isResumePolicy(v: unknown): v is ResumePolicy {
  return typeof v === 'string' && (RESUME_POLICIES as readonly string[]).includes(v);
}

/** The Even host's storage bridge — the one store that reliably survives app
 * restarts in the Flutter WebView, and the one shared between the dev-server
 * and packed-build origins (which hold separate browser localStorage). */
export interface SettingsHostStorage {
  get(key: string): Promise<string>;
  set(key: string, value: string): Promise<boolean>;
}

/**
 * Wearer-adjustable settings, persisted in the WebView's localStorage so they
 * survive plugin relaunches. Storage can be absent or blocked (fresh install,
 * private mode) — every access is guarded and the defaults below apply.
 *
 * Durability follows the same rule as pairing: browser localStorage is only a
 * fast local mirror, while `attachHostStorage` write-throughs the Even host's
 * storage, which Android WebView kills and origin switches cannot drop.
 *
 * Default resume policy is 'confirm': fail-safe (mic never hot-swaps on
 * unasked) and fully functional without the Mac brain running.
 */
export class Settings {
  private resume: ResumePolicy = 'confirm';
  private listener?: (policy: ResumePolicy) => void;
  private host?: SettingsHostStorage;
  /** A policy the wearer (or Mac) chose this session outranks a hosted echo. */
  private chosenThisSession = false;

  /** Status surfaces (webview panel) subscribe to see menu-driven changes. */
  onChange(listener: (policy: ResumePolicy) => void): void {
    this.listener = listener;
  }

  load(): void {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const parsed: unknown = JSON.parse(raw);
      const candidate = (parsed as { resume?: unknown }).resume;
      if (isResumePolicy(candidate)) this.resume = candidate;
    } catch {
      // Unreadable storage → keep defaults.
    }
  }

  /**
   * Adopt the Even host's durable value, then write-through on every change.
   * The hosted value wins over a stale local mirror (a reinstall or the other
   * delivery-path origin), but never over a policy already chosen live in
   * this session. Failures leave the local mirror authoritative.
   */
  async attachHostStorage(host: SettingsHostStorage): Promise<void> {
    this.host = host;
    let raw: string;
    try {
      raw = await host.get(STORAGE_KEY);
    } catch {
      return;
    }
    if (this.chosenThisSession) {
      // A live choice outranks the hosted echo — and the host must converge
      // on it now, not at the next change: a choice made before this attach
      // (or whose write-through failed) would otherwise resurrect the stale
      // hosted policy at the next boot.
      void host.set(STORAGE_KEY, JSON.stringify({ resume: this.resume }))
        .then(() => undefined, () => undefined);
      return;
    }
    if (!raw) return;
    try {
      const parsed: unknown = JSON.parse(raw);
      const candidate = (parsed as { resume?: unknown }).resume;
      if (isResumePolicy(candidate) && candidate !== this.resume) {
        this.resume = candidate;
        this.listener?.(candidate);
        try {
          localStorage.setItem(STORAGE_KEY, JSON.stringify({ resume: candidate }));
        } catch {
          // The local mirror is best-effort; the hosted copy is authoritative.
        }
      }
    } catch {
      // Malformed hosted value — keep the current policy.
    }
  }

  get resumePolicy(): ResumePolicy {
    return this.resume;
  }

  setResumePolicy(policy: ResumePolicy): void {
    this.resume = policy;
    this.chosenThisSession = true;
    this.listener?.(policy);
    const encoded = JSON.stringify({ resume: this.resume });
    try {
      localStorage.setItem(STORAGE_KEY, encoded);
    } catch {
      // Unwritable storage → setting holds for this session only.
    }
    void this.host?.set(STORAGE_KEY, encoded).then(() => undefined, () => undefined);
  }
}
