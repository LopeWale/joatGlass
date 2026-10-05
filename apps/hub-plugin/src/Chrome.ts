import { ContainerId } from './types';
import type { HudPresenter } from './HudPresenter';
import type { LinkState } from './MacLink';
import type { HudStatusModel } from './HudStatus';

/**
 * Plugin-owned chrome: the parts of the HUD that must render WITHOUT the Mac.
 *
 * The dashboard design gave the plugin the status bar and the hint line for
 * exactly this reason, but only the containers were built — nothing ever
 * painted into them. On real glasses that read as a dead assistant: one
 * status line on an otherwise blank canvas, with no clock, no battery, and
 * no clue what any gesture does. (Wearer, on 0.1.5: "all I see is JOAT ·
 * listening".)
 *
 * Everything here is derived from state the PHONE already has, so it keeps
 * updating through a Mac outage — which is the whole point.
 *
 * Chrome hands the presenter a `HudStatusModel`, not a composed line: the
 * raster spine draws icons from its fields and the firmware pages compose
 * their one-line header from it (HudStatus.ts), so nothing downstream has
 * to parse words back into facts.
 */
// The resting overview's gestures, truthfully: tap expands the tab the
// rail has lit, swipe moves along the rail, hold is the microphone. The
// menu is the firmware's (tap, then hold) and needs no line of its own.
const HINT = 'tap: open · swipe: tabs · hold: mic · “JOAT …”';
const RESTING_WORDS = /^(?:listening|mic off|ready|online)$/i;

export class Chrome {
  private timer?: ReturnType<typeof setInterval>;
  private link: LinkState = 'connecting';
  private mic = false;
  private battery?: number;
  private charging = false;
  private macState?: string;
  private macStateTimer?: ReturnType<typeof setTimeout>;
  private active = true;
  private paintGeneration = 0;

  constructor(private readonly hud: HudPresenter) {}

  /** Paint now, then keep the clock honest. */
  start(): void {
    if (this.timer) clearInterval(this.timer);
    this.paintGeneration += 1;
    this.active = true;
    // Claim the status bar: every setStatus() anywhere becomes this
    // Chrome's state word, so container 1 has exactly one writer.
    this.hud.onStatus = (text) => this.setMacState(text);
    void this.paint();
    // 30s: the clock shows minutes, so this is never more than a minute
    // stale, and it is far below any sane BLE budget.
    this.timer = setInterval(() => void this.paint(), 30_000);
  }

  stop(): void {
    this.active = false;
    this.paintGeneration += 1;
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
    if (this.macStateTimer) clearTimeout(this.macStateTimer);
    this.macStateTimer = undefined;
    this.macState = undefined;
  }

  setLink(state: LinkState): void {
    this.link = state;
    void this.paint();
  }

  /**
   * The Mac's transient state word ("hearing you…", "online"). The status
   * bar has ONE owner — this class — so the Mac supplies the middle slot
   * rather than painting the line, and the clock and battery survive.
   */
  setMacState(text: string): void {
    if (!this.active) return;
    const word = text.replace(/^JOAT\s*[·-]?\s*/i, '').trim();
    // "listening", "mic off" and "ready" restate what the microphone glyph
    // and icon already show; as a state word they would hide the page's
    // name in the spine. They mean rest, so they clear the word.
    this.macState = word && !RESTING_WORDS.test(word) ? word : undefined;
    void this.paint();
    // Transient by nature: fall back to the derived state so the bar cannot
    // latch a stale word (a latched status is what stranded the wearer once).
    if (this.macStateTimer) clearTimeout(this.macStateTimer);
    this.macStateTimer = setTimeout(() => {
      this.macState = undefined;
      void this.paint();
    }, 12_000);
  }

  setMic(open: boolean): void {
    this.mic = open;
    void this.paint();
  }

  setBattery(level?: number, charging = false): void {
    this.battery = level;
    this.charging = charging;
    void this.paint();
  }

  private clock(): string {
    const d = new Date();
    const h = d.getHours() % 12 || 12;
    return `${h}:${String(d.getMinutes()).padStart(2, '0')}`;
  }

  /** The facts as they stand, for the presenter to draw however the page
   * it holds can draw them. */
  model(): HudStatusModel {
    return {
      clock: this.clock(),
      link: this.link,
      mic: this.mic,
      ...(this.battery !== undefined ? { battery: this.battery } : {}),
      charging: this.charging,
      ...(this.macState ? { macState: this.macState } : {}),
    };
  }

  private async paint(): Promise<void> {
    if (!this.active) return;
    const generation = this.paintGeneration;
    await this.hud.setStatusModel(this.model());
    if (!this.active || generation !== this.paintGeneration) return;
    await this.hud.showText(HINT, ContainerId.Hint);
  }
}
