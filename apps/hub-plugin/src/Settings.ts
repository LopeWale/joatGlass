import type { MicMode, ResumePolicy } from './types';

const STORAGE_KEY = 'joat.settings.v1';

const RESUME_POLICIES: readonly ResumePolicy[] = ['auto', 'mac', 'confirm'];
const MIC_MODES: readonly MicMode[] = ['glasses', 'phone', 'on_demand'];

function isResumePolicy(v: unknown): v is ResumePolicy {
  return typeof v === 'string' && (RESUME_POLICIES as readonly string[]).includes(v);
}

function isMicMode(v: unknown): v is MicMode {
  return typeof v === 'string' && (MIC_MODES as readonly string[]).includes(v);
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
  private mic: MicMode = 'glasses';
  private listener?: (policy: ResumePolicy) => void;
  private micListener?: (mode: MicMode) => void;
  private host?: SettingsHostStorage;
  /** A policy the wearer (or Mac) chose this session outranks a hosted echo. */
  private chosenThisSession = false;
  private chosenMicThisSession = false;

  /** Status surfaces (webview panel) subscribe to see menu-driven changes. */
  onChange(listener: (policy: ResumePolicy) => void): void {
    this.listener = listener;
  }

  onMicModeChange(listener: (mode: MicMode) => void): void {
    this.micListener = listener;
  }

  private serialized(): string {
    return JSON.stringify(
      this.mic === 'glasses'
        ? { resume: this.resume }
        : { resume: this.resume, micMode: this.mic },
    );
  }

  load(): void {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const parsed: unknown = JSON.parse(raw);
      const candidate = (parsed as { resume?: unknown }).resume;
      if (isResumePolicy(candidate)) this.resume = candidate;
      const micCandidate = (parsed as { micMode?: unknown }).micMode;
      if (isMicMode(micCandidate)) this.mic = micCandidate;
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
    if (this.chosenThisSession && this.chosenMicThisSession) {
      void host.set(STORAGE_KEY, this.serialized())
        .then(() => undefined, () => undefined);
      return;
    }
    if (!raw) {
      if (this.chosenThisSession || this.chosenMicThisSession) {
        void host.set(STORAGE_KEY, this.serialized())
          .then(() => undefined, () => undefined);
      }
      return;
    }
    try {
      const parsed: unknown = JSON.parse(raw);
      const candidate = (parsed as { resume?: unknown }).resume;
      const micCandidate = (parsed as { micMode?: unknown }).micMode;
      let changed = false;
      if (!this.chosenThisSession && isResumePolicy(candidate) && candidate !== this.resume) {
        this.resume = candidate;
        this.listener?.(candidate);
        changed = true;
      }
      if (!this.chosenMicThisSession && isMicMode(micCandidate) && micCandidate !== this.mic) {
        this.mic = micCandidate;
        this.micListener?.(micCandidate);
        changed = true;
      }
      if (changed) {
        try {
          localStorage.setItem(STORAGE_KEY, this.serialized());
        } catch {
          // The local mirror is best-effort; the hosted copy is authoritative.
        }
      }
      if (this.chosenThisSession || this.chosenMicThisSession) {
        void host.set(STORAGE_KEY, this.serialized())
          .then(() => undefined, () => undefined);
      }
    } catch {
      // Malformed hosted value — keep the current policy.
      if (this.chosenThisSession || this.chosenMicThisSession) {
        void host.set(STORAGE_KEY, this.serialized())
          .then(() => undefined, () => undefined);
      }
    }
  }

  get resumePolicy(): ResumePolicy {
    return this.resume;
  }

  get micMode(): MicMode {
    return this.mic;
  }

  setResumePolicy(policy: ResumePolicy): void {
    this.resume = policy;
    this.chosenThisSession = true;
    this.listener?.(policy);
    const encoded = this.serialized();
    try {
      localStorage.setItem(STORAGE_KEY, encoded);
    } catch {
      // Unwritable storage → setting holds for this session only.
    }
    void this.host?.set(STORAGE_KEY, encoded).then(() => undefined, () => undefined);
  }

  setMicMode(mode: MicMode): void {
    this.mic = mode;
    this.chosenMicThisSession = true;
    this.micListener?.(mode);
    const encoded = this.serialized();
    try {
      localStorage.setItem(STORAGE_KEY, encoded);
    } catch {
      // Unwritable storage → setting holds for this session only.
    }
    void this.host?.set(STORAGE_KEY, encoded).then(() => undefined, () => undefined);
  }
}
