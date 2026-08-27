import { OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { HudPresenter } from './HudPresenter';
import type { Settings } from './Settings';
import type { MacLink } from './MacLink';
import type { ResumePolicy } from './types';

type ListEvent = NonNullable<EvenHubEvent['listEvent']>;

/** Menu rows, in display order. ASCII-only labels — 4-bit HUD font, 64-char cap. */
const ROWS: ReadonlyArray<{ policy: ResumePolicy; label: string }> = [
  { policy: 'auto', label: 'Resume mic: instantly' },
  { policy: 'mac', label: 'Resume mic: Mac decides' },
  { policy: 'confirm', label: 'Resume mic: ask me first' },
];

/**
 * On-glasses settings page. Opened/closed by left-temple double-tap (routed
 * here by EventRouter); rendered as the standard list layout; a click on a
 * row applies that policy, persists it, tells the Mac, and closes.
 *
 * While open, list events are consumed locally — the Mac never sees the
 * wearer navigating settings.
 */
export class SettingsMenu {
  private open = false;
  /** Live mic state, injected so the restored status line tells the truth. */
  micIsOpen: () => boolean = () => false;

  constructor(
    private readonly hud: HudPresenter,
    private readonly settings: Settings,
    private readonly link: MacLink,
  ) {}

  isOpen(): boolean {
    return this.open;
  }

  toggle(): void {
    if (this.open) {
      void this.close('JOAT · listening');
    } else {
      this.open = true;
      void this.hud.showList(ROWS.map((r) => this.label(r)));
      void this.hud.setStatus('JOAT · settings (tap to pick)');
    }
  }

  /** Returns true when the event was consumed (menu open). */
  handleList(list: ListEvent): boolean {
    if (!this.open) return false;

    // `?? 0` on both reads: the protobuf transport omits zero-valued fields,
    // so CLICK_EVENT (0) and the first row's index (0) both arrive as
    // `undefined`. Without this the top row is unselectable and no click
    // registers at all.
    if ((list.eventType ?? 0) === OsEventTypeList.CLICK_EVENT) {
      const row = ROWS[list.currentSelectItemIndex ?? 0];
      if (row) {
        this.settings.setResumePolicy(row.policy);
        this.link.send({ kind: 'policy', resume: row.policy });
        void this.close(`JOAT · ${row.label.toLowerCase()}`);
      }
    }
    // Scrolls and other list events while open: host handles highlight; we
    // just keep them off the uplink.
    return true;
  }

  private label(row: (typeof ROWS)[number]): string {
    const active = row.policy === this.settings.resumePolicy;
    return `${active ? '*' : ' '} ${row.label}`;
  }

  private async close(_status: string): Promise<void> {
    this.open = false;
    await this.hud.showDefault();
    // Restore a LIVE home, not a souvenir of the menu. Latching the picked
    // row's label as the status left a blank page + stale line that read as
    // a dead assistant, with no way out short of killing the phone app
    // (observed on hardware, wearer's words: "just bad at the moment").
    await this.hud.setStatus(
      this.micIsOpen() ? 'JOAT · listening' : 'JOAT · mic off — long-press: menu');
    // Tell the Mac the wearer is back so it repaints the content regions —
    // the rebuild cleared them, and a silent Mac means a blank screen.
    this.link.send({ kind: 'input', event: { containerName: 'menu', selectedName: 'closed' } });
  }
}
