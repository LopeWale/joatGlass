import { ContainerId } from './types';
import type { HudPresenter } from './HudPresenter';
import type { LinkState } from './MacLink';

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
 */
const HINT = 'left x2: menu · long-press: mic · \u201cJOAT \u2026\u201d';
// The visual renderer centers this text between a fixed clock and microphone
// region.  Thirty source characters become at most a 32-character rendered
// state after its JOAT separator, which leaves the microphone/battery chrome
// clear on the 576px optics.
const STATUS_STATE_MAX_CHARS = 30;

export class Chrome {
  private timer?: ReturnType<typeof setInterval>;
  private link: LinkState = 'connecting';
  private mic = false;
  private battery?: number;
  private charging = false;
  private macState?: string;
  private macStateTimer?: ReturnType<typeof setTimeout>;

  constructor(private readonly hud: HudPresenter) {}

  /** Paint now, then keep the clock honest. */
  start(): void {
    // Claim the status bar: every setStatus() anywhere becomes this
    // Chrome's state word, so container 1 has exactly one writer.
    this.hud.onStatus = (text) => this.setMacState(text);
    void this.paint();
    // 30s: the clock shows minutes, so this is never more than a minute
    // stale, and it is far below any sane BLE budget.
    this.timer = setInterval(() => void this.paint(), 30_000);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
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
    this.macState = text.replace(/^JOAT\s*[·-]?\s*/i, '').trim() || undefined;
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
    this.hud.setMicState(open);
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

  private state(): string {
    // The LINK matters more than the mic: a wearer whose brain is gone needs
    // to know that, not that the microphone is dutifully recording nothing.
    if (this.link !== 'connected') return `JOAT ${this.link}`;
    if (this.macState) return `JOAT ${this.macState}`;
    return this.mic ? 'JOAT listening' : 'JOAT mic off';
  }

  private async paint(): Promise<void> {
    const left = this.clock();
    const right = this.battery !== undefined
      ? `bat ${this.battery}%${this.charging ? '+' : ''}`
      : '';
    // The status wire format is limited to 46 characters, but `slice()` at
    // the end used to cut the battery suffix off first. The visual renderer
    // then mistook the partial `bat 10` as state text and centered it through
    // the microphone icon. Reserve stable chrome first; truncate only the
    // semantic state that can safely be shortened.
    const middleBudget = Math.max(
      0,
      Math.min(
        STATUS_STATE_MAX_CHARS,
        46 - left.length - 1 - (right ? right.length + 1 : 0),
      ),
    );
    const state = this.state();
    const middle = state.length <= middleBudget
      ? state
      : `${state.slice(0, Math.max(0, middleBudget - 1)).trimEnd()}…`;
    const line = right ? `${left} ${middle} ${right}` : `${left} ${middle}`;
    await this.hud.showText(line, ContainerId.StatusBar);
    await this.hud.showText(HINT, ContainerId.Hint);
  }
}
