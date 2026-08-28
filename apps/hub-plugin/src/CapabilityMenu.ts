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

const SETTINGS_SURFACE = {
  title: 'LISTENING RESUME',
  hint: 'tap apply · left x2 overview',
} as const;

export class CapabilityMenu {
  private view: 'closed' | 'caps' | 'settings' | 'data' = 'closed';
  private selectedIndex = 0;
  private pendingTitle?: string;
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

  /** A capability response is valid only for the data view that requested it.
   * This prevents a slow Mac reply from reopening a dismissed page or
   * replacing a newer capability selection. */
  acceptsList(title?: string): boolean {
    if (this.view !== 'data' || !this.pendingTitle || !title) return false;
    const normalized = title.trim().toUpperCase();
    return normalized === this.pendingTitle || normalized.startsWith(`${this.pendingTitle} ·`);
  }

  /** Left double-tap. Closed: open the capability menu. Anywhere else: BACK
   *  home — one gesture, one meaning, matching the hint line. */
  async toggle(): Promise<void> {
    if (this.view === 'closed') {
      this.view = 'caps';
      this.selectedIndex = 0;
      const shown = await this.hud.showSidebar([...CAPABILITIES], this.selectedIndex);
      if (!shown && this.view === 'caps') this.view = 'closed';
    } else {
      await this.close();
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
    this.selectedIndex = index;
    return this.select(index);
  }

  /** Custom image surfaces use the transparent text capture layer, so JOAT
   * owns selection movement instead of the firmware List widget. */
  handleScroll(eventType: number): boolean {
    if (this.view === 'closed') return false;
    const delta = eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT
      ? 1
      : eventType === OsEventTypeList.SCROLL_TOP_EVENT
        ? -1
        : 0;
    if (delta === 0) return true;
    if (this.view === 'data') {
      void this.hud.moveSelection(delta);
      return true;
    }
    const rows = this.view === 'settings' ? POLICY_ROWS : CAPABILITIES;
    this.selectedIndex = Math.max(0, Math.min(rows.length - 1, this.selectedIndex + delta));
    void this.hud.setSelection(this.selectedIndex);
    return true;
  }

  /** Right temple/ring click selects the highlighted custom-rendered row. */
  handleClick(): boolean {
    if (this.view === 'closed' || this.view === 'data') return false;
    return this.select(this.selectedIndex);
  }

  private select(index: number): boolean {
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
      this.pendingTitle = undefined;
      this.view = 'settings';
      this.selectedIndex = 0;
      void this.hud.showList(POLICY_ROWS.map((r) => this.label(r)), SETTINGS_SURFACE);
      return true;
    }
    if (picked) {
      // The data lives on the Mac; ask it to draw the view. The Mac answers
      // with hud.list / hud.text, which lands in the 'data' state where
      // double-tap still means back.
      this.view = 'data';
      this.pendingTitle = picked.toUpperCase();
      void this.hud.showList([`Loading recent ${picked.toLowerCase()}…`], {
        title: picked.toUpperCase(),
        hint: 'left x2 overview',
      });
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
    this.selectedIndex = 0;
    this.pendingTitle = undefined;
    // Restore a LIVE home, not a souvenir of the menu — a stale status line
    // over a blank page reads as a dead assistant (observed on hardware).
    await this.hud.showDefault();
    await this.hud.setStatus(this.micIsOpen() ? 'JOAT · listening' : 'JOAT · mic off');
    // HudPresenter restores the buffered dashboard atomically. Asking the Mac
    // to repaint here used to overwrite a newer card with an older last_reply.
  }
}
