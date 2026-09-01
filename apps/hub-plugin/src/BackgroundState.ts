/**
 * Survive the host's background migration.
 *
 * Backgrounding the phone is not a pause. Per Even Realities' background-state
 * documentation the Even Hub host, on `inactive`, snapshots the page by calling
 * `window.__getStateSnapshot()`; on `paused` it builds a NEW HeadlessInAppWebView,
 * loads the same plugin URL, and replays the snapshot through
 * `window.__restoreState(...)`; on `resumed` it injects the snapshot into the
 * foreground WebView and destroys the headless one.
 *
 * The consequence is stated plainly in that document: a plugin that registers
 * no exporters gets `'{}'` back, "causing the plugin to reset to its initial
 * state every time the phone comes back from the background". JOAT registered
 * none, so every pocket-and-return rebooted the page and dropped the wearer
 * back on Overview mid-task.
 *
 * The documented `setBackgroundState`/`onBackgroundRestore` helpers are NOT
 * exported by the pinned SDK (checked against
 * `node_modules/@evenrealities/even_hub_sdk` 0.0.14, which is also the newest
 * published version), and CLAUDE.md makes the installed type definitions the
 * ground truth over the skill text. So this module implements the host contract
 * directly against the two `window` globals the host actually calls, behind the
 * documented function names — when the SDK ships them, delete this file and
 * change the import.
 */

export type BackgroundSnapshot = Record<string, unknown>;

type Exporter = () => BackgroundSnapshot;
type Restorer = (saved: BackgroundSnapshot) => void;

const exporters = new Map<string, Exporter>();
const restorers = new Map<string, Restorer>();

interface HostStateBridge {
  __getStateSnapshot?: () => string;
  __restoreState?: (snapshot: string | BackgroundSnapshot) => void;
}

function host(): HostStateBridge {
  return globalThis as unknown as HostStateBridge;
}

/** Collect every registered slice into one JSON string for the host. */
function snapshot(): string {
  const state: Record<string, BackgroundSnapshot> = {};
  for (const [key, exporter] of exporters) {
    try {
      state[key] = exporter();
    } catch (error) {
      // One bad slice must not cost the wearer every other slice, and an
      // exception thrown into the host's snapshot call is worse still.
      console.error(`[joat] background export failed for ${key}:`, error);
    }
  }
  try {
    return JSON.stringify(state);
  } catch (error) {
    console.error('[joat] background snapshot is not serializable:', error);
    return '{}';
  }
}

/** Replay a host snapshot into the freshly loaded page. */
function restore(raw: string | BackgroundSnapshot): void {
  let parsed: unknown = raw;
  if (typeof raw === 'string') {
    try {
      parsed = JSON.parse(raw || '{}');
    } catch (error) {
      console.error('[joat] background snapshot was not valid JSON:', error);
      return;
    }
  }
  if (typeof parsed !== 'object' || parsed === null) return;
  for (const [key, restorer] of restorers) {
    const slice = (parsed as Record<string, unknown>)[key];
    if (typeof slice !== 'object' || slice === null) continue;
    try {
      restorer(slice as BackgroundSnapshot);
    } catch (error) {
      console.error(`[joat] background restore failed for ${key}:`, error);
    }
  }
}

let installed = false;

/** Publish the two globals the host calls. Idempotent. */
export function installBackgroundStateBridge(): void {
  if (installed) return;
  installed = true;
  host().__getStateSnapshot = snapshot;
  host().__restoreState = restore;
}

/**
 * Register one slice of state to carry across a background migration.
 *
 * The exporter must return a plain JSON-serializable object — no Maps, Sets,
 * Dates, class instances, or functions — and must return a COPY, because the
 * host reads it later than you register it.
 */
export function setBackgroundState(key: string, exporter: Exporter): void {
  installBackgroundStateBridge();
  exporters.set(key, exporter);
}

/** Register the matching restorer. The key must equal the exporter's key. */
export function onBackgroundRestore(key: string, restorer: Restorer): void {
  installBackgroundStateBridge();
  restorers.set(key, restorer);
}

/** Test seam: forget every registration and uninstall the globals. */
export function resetBackgroundStateForTest(): void {
  exporters.clear();
  restorers.clear();
  installed = false;
  delete host().__getStateSnapshot;
  delete host().__restoreState;
}

/** Test seam: what the host would receive right now. */
export function captureBackgroundSnapshotForTest(): string {
  return snapshot();
}
