import { OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { HudPresenter } from './HudPresenter';
import type { Settings } from './Settings';
import type { MacLink } from './MacLink';
import type { ResumePolicy } from './types';

type ListEvent = NonNullable<EvenHubEvent['listEvent']>;

/**
 * The control-center navigation from the approved dashboard design:
 * left double-tap opens the CAPABILITY menu — Messages, Notifications,
 * Calendar, Debate, Settings — and each entry opens its data view. The
 * first build shipped only the old resume-policy list here, so the wearer
 * double-tapped into "a menu modal, but not the one we designed" (their
 * words, on hardware). Settings still exists — one level down.
 *
 * View machine, and what double-tap means in each state:
 *   home ──double-tap──▶ caps ──pick row──▶ data view (Mac-drawn) or settings
 *   caps/settings/data ──double-tap──▶ home  (double-tap is BACK, always)
 */
const CAPABILITIES = ['Messages', 'Notifications', 'Calendar', 'Debate', 'Settings'] as const;

const POLICY_ROWS: ReadonlyArray<{ policy: ResumePolicy; label: string }> = [
  { policy: 'auto', label: 'Resume mic: instantly' },
  { policy: 'mac', label: 'Resume mic: Mac decides' },
  { policy: 'confirm', label: 'Resume mic: ask me first' },
];

export class CapabilityMenu {
  private view: 'closed' | 'caps' | 'settings' | 'data' = 'closed';
  /** Live mic state, injected so the restored status line tells the truth. */
  micIsOpen: () => boolean = () => false;

  constructor(
    private readonly hud: HudPresenter,
    private readonly settings: Settings,
    private readonly link: MacLink,
  ) {}

  isOpen(): boolean {
    return this.view !== 'closed';
  }

  /** Left double-tap. Closed: open the capability menu. Anywhere else: BACK
   *  home — one gesture, one meaning, matching the hint line. */
  toggle(): void {
    if (this.view === 'closed') {
      this.view = 'caps';
      void this.hud.showList([...CAPABILITIES]);
      void this.hud.setStatus('JOAT · menu');
    } else {
      void this.close();
    }
  }

  /** Returns true when the event was consumed (a menu view is open). */
  handleList(list: ListEvent): boolean {
    if (this.view === 'closed' || this.view === 'data') return false;

    // `?? 0` on both reads: the protobuf transport omits zero-valued fields,
    // so CLICK_EVENT (0) and the first row's index (0) both arrive as
    // `undefined`. Without this the top row is unselectable.
    if ((list.eventType ?? 0) !== OsEventTypeList.CLICK_EVENT) {
      return true; // scrolls: host moves the highlight; keep them off the uplink
    }
    const index = list.currentSelectItemIndex ?? 0;

    if (this.view === 'settings') {
      const row = POLICY_ROWS[index];
      if (row) {
        this.settings.setResumePolicy(row.policy);
        this.link.send({ kind: 'policy', resume: row.policy });
        void this.close();
      }
      return true;
    }

    const picked = CAPABILITIES[index];
    if (picked === 'Settings') {
      this.view = 'settings';
      void this.hud.showList(POLICY_ROWS.map((r) => this.label(r)));
      void this.hud.setStatus('JOAT · settings (tap to pick)');
      return true;
    }
    if (picked) {
      // The data lives on the Mac; ask it to draw the view. The Mac answers
      // with hud.list / hud.text, which lands in the 'data' state where
      // double-tap still means back.
      this.view = 'data';
      this.link.send({
        kind: 'input',
        event: { containerName: 'capmenu', eventType: 0, selectedIndex: index, selectedName: picked },
      });
    }
    return true;
  }

  private label(row: (typeof POLICY_ROWS)[number]): string {
    const active = row.policy === this.settings.resumePolicy;
    return `${active ? '*' : ' '} ${row.label}`;
  }

  private async close(): Promise<void> {
    this.view = 'closed';
    await this.hud.showDefault();
    // Restore a LIVE home, not a souvenir of the menu — a stale status line
    // over a blank page reads as a dead assistant (observed on hardware).
    await this.hud.setStatus(this.micIsOpen() ? 'JOAT · listening' : 'JOAT · mic off');
    // Tell the Mac the wearer is back so it repaints the content regions —
    // the rebuild cleared them, and a silent Mac means a blank screen.
    this.link.send({ kind: 'input', event: { containerName: 'menu', selectedName: 'closed' } });
  }
}
