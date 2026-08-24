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

    if (list.eventType === OsEventTypeList.CLICK_EVENT) {
      const row = ROWS[list.currentSelectItemIndex ?? -1];
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

  private async close(status: string): Promise<void> {
    this.open = false;
    await this.hud.showDefault();
    await this.hud.setStatus(status);
  }
}
