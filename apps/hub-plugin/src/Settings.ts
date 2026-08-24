import type { ResumePolicy } from './types';

const STORAGE_KEY = 'joat.settings.v1';

const RESUME_POLICIES: readonly ResumePolicy[] = ['auto', 'mac', 'confirm'];

function isResumePolicy(v: unknown): v is ResumePolicy {
  return typeof v === 'string' && (RESUME_POLICIES as readonly string[]).includes(v);
}

/**
 * Wearer-adjustable settings, persisted in the WebView's localStorage so they
 * survive plugin relaunches. Storage can be absent or blocked (fresh install,
 * private mode) — every access is guarded and the defaults below apply.
 *
 * Default resume policy is 'confirm': fail-safe (mic never hot-swaps on
 * unasked) and fully functional without the Mac brain running.
 */
export class Settings {
  private resume: ResumePolicy = 'confirm';
  private listener?: (policy: ResumePolicy) => void;

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

  get resumePolicy(): ResumePolicy {
    return this.resume;
  }

  setResumePolicy(policy: ResumePolicy): void {
    this.resume = policy;
    this.listener?.(policy);
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ resume: this.resume }));
    } catch {
      // Unwritable storage → setting holds for this session only.
    }
  }
}
