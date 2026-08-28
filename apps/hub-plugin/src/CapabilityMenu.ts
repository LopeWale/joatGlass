import { OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { HudPresenter } from './HudPresenter';
import type { Settings } from './Settings';
import type { MacLink } from './MacLink';
import type { ResumePolicy } from './types';

type ListEvent = NonNullable<EvenHubEvent['listEvent']>;

/**
 * The control-center navigation from the approved dashboard design:
 * left double-tap opens the CAPABILITY menu — Messages, Notifications,
 * Calendar, Tasks, Brainstorm — and each entry opens its data view. The
 * first build shipped only the old resume-policy list here, so the wearer
 * double-tapped into "a menu modal, but not the one we designed" (their
 * words, on hardware). Settings still exists — one level down.
 *
 * View machine, and what double-tap means in each state:
 *   home ──double-tap──▶ caps ──pick row──▶ data view (Mac-drawn) or settings
 *   caps/settings/data ──double-tap──▶ home  (double-tap is BACK, always)
 */
const CAPABILITIES = ['Messages', 'Notifications', 'Calendar', 'Tasks', 'Brainstorm'] as const;

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
  private dataItemCount = 0;
  private dataMessageSelectable = false;
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
  acceptsList(title?: string, itemCount = 0): boolean {
    if (this.view !== 'data' || !this.pendingTitle || !title) return false;
    const normalized = title.trim().toUpperCase();
    const accepted = normalized === this.pendingTitle
      || normalized.startsWith(`${this.pendingTitle} ·`);
    if (accepted) {
      // Only the raw Messages inbox contains actionable conversation rows.
      // Setup pages, other capabilities, and MESSAGES · REPLY instructions
      // are display-only and must never be turned into recipient selection.
      const selectableMessages = this.pendingTitle === 'MESSAGES'
        && normalized === 'MESSAGES';
      this.dataItemCount = Math.max(0, itemCount);
      this.dataMessageSelectable = selectableMessages;
      this.selectedIndex = 0;
    }
    return accepted;
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
      const maximum = Math.max(0, this.dataItemCount - 1);
      const next = Math.max(0, Math.min(maximum, this.selectedIndex + delta));
      const applied = next - this.selectedIndex;
      this.selectedIndex = next;
      if (applied !== 0) void this.hud.moveSelection(applied);
      return true;
    }
    const rows = this.view === 'settings' ? POLICY_ROWS : CAPABILITIES;
    this.selectedIndex = Math.max(0, Math.min(rows.length - 1, this.selectedIndex + delta));
    void this.hud.setSelection(this.selectedIndex);
    return true;
  }

  /** Right temple/ring click selects the highlighted custom-rendered row. */
  handleClick(): boolean {
    if (this.view === 'closed') return false;
    if (this.view === 'data') {
      if (!this.dataMessageSelectable || this.dataItemCount === 0) return true;
      const random = globalThis.crypto?.randomUUID?.()
        ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      const traceId = `hud-${random}`;
      console.info(
        `[joat trace] trace=${traceId} stage=message-selected index=${this.selectedIndex}`,
      );
      this.link.send({
        kind: 'input',
        event: {
          containerName: 'capdata',
          selectedIndex: this.selectedIndex,
          selectedName: this.pendingTitle,
          traceId,
        },
      });
      return true;
    }
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
      this.dataItemCount = 0;
      this.dataMessageSelectable = false;
      this.selectedIndex = 0;
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
    this.dataItemCount = 0;
    this.dataMessageSelectable = false;
    // Restore a LIVE home, not a souvenir of the menu — a stale status line
    // over a blank page reads as a dead assistant (observed on hardware).
    await this.hud.showDefault();
    await this.hud.setStatus(this.micIsOpen() ? 'JOAT · listening' : 'JOAT · mic off');
    // HudPresenter restores the buffered dashboard atomically. Asking the Mac
    // to repaint here used to overwrite a newer card with an older last_reply.
  }
}
