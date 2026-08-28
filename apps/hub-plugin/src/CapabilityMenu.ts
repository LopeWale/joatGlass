import { OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { HudPresenter } from './HudPresenter';
import type { Settings } from './Settings';
import type { MacLink } from './MacLink';
import type { ResumePolicy } from './types';

type ListEvent = NonNullable<EvenHubEvent['listEvent']>;

/**
 * The control-center navigation from the approved dashboard design:
 * left double-tap opens the CAPABILITY menu — Messages, Notifications,
 * Calendar, Tasks, More — and each entry opens its data view. The
 * first build shipped only the old resume-policy list here, so the wearer
 * double-tapped into "a menu modal, but not the one we designed" (their
 * words, on hardware). Settings still exists — one level down.
 *
 * View machine, and what double-tap means in each state:
 *   home ──double-tap──▶ caps ──pick row──▶ data view (Mac-drawn) or settings
 *   caps/settings/data ──double-tap──▶ home  (double-tap is BACK, always)
 */
const CAPABILITIES = ['Messages', 'Notifications', 'Calendar', 'Tasks', 'More'] as const;
const MORE_CAPABILITIES = ['QuickNotes', 'Brainstorm', 'Activity'] as const;

const POLICY_ROWS: ReadonlyArray<{ policy: ResumePolicy; label: string }> = [
  { policy: 'auto', label: 'Resume mic: instantly' },
  { policy: 'mac', label: 'Resume mic: Mac decides' },
  { policy: 'confirm', label: 'Resume mic: ask me first' },
];

const SETTINGS_SURFACE = {
  title: 'LISTENING RESUME',
  hint: 'tap apply · left x2 overview',
} as const;

export function isActionLifecycleTitle(title?: string): boolean {
  return title?.trim().toUpperCase().startsWith('ACTION ·') ?? false;
}

export class CapabilityMenu {
  private view: 'closed' | 'transition' | 'caps' | 'more' | 'settings' | 'data' = 'closed';
  private selectedIndex = 0;
  private dataItemCount = 0;
  private dataRowsSelectable = false;
  private pendingTitle?: string;
  private pendingRequestId?: string;
  private transitionId = 0;
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

  /** A live session owns the overview, not the menu page that launched it.
   * Clear input ownership synchronously before rebuilding so the first
   * streamed frame cannot remain trapped behind the Brainstorm instructions. */
  async beginLiveSession(session: 'brainstorm'): Promise<void> {
    void session;
    this.transitionId += 1;
    this.view = 'closed';
    this.selectedIndex = 0;
    this.pendingTitle = undefined;
    this.pendingRequestId = undefined;
    this.dataItemCount = 0;
    this.dataRowsSelectable = false;
    await this.hud.showDefault();
  }

  /** Contextual-menu entry: settings remains reachable without consuming one
   * of the five 576x288 sidebar rows. */
  async openSettings(): Promise<void> {
    this.pendingTitle = undefined;
    this.pendingRequestId = undefined;
    const transition = this.beginTransition();
    this.selectedIndex = 0;
    const shown = await this.hud.showList(
      POLICY_ROWS.map((row) => this.label(row)),
      SETTINGS_SURFACE,
    );
    if (!this.isCurrentTransition(transition)) return;
    if (shown) this.view = 'settings';
    else await this.close();
  }

  /** Match, paint, then activate one capability response. Row actions remain
   * disabled until the replacement frame is completely visible. */
  async presentList(
    title?: string,
    items: string[] = [],
    requestId?: string,
    selectable = false,
    hint?: string,
  ): Promise<boolean> {
    if (this.view !== 'data' || !this.pendingTitle || !title) return false;
    if (!this.pendingRequestId || requestId !== this.pendingRequestId) return false;
    const normalized = title.trim().toUpperCase();
    const matches = normalized === this.pendingTitle
      || normalized.startsWith(`${this.pendingTitle} ·`);
    if (!matches) return false;

    const expectedRequestId = this.pendingRequestId;
    const transition = this.beginTransition();
    this.dataRowsSelectable = false;
    this.dataItemCount = 0;
    const shown = await this.hud.showList(items, { title, hint, selectable });
    if (
      !this.isCurrentTransition(transition)
      || this.pendingRequestId !== expectedRequestId
    ) {
      return true;
    }
    if (!shown) {
      await this.close();
      return true;
    }
    this.view = 'data';
    this.dataItemCount = items.length;
    this.dataRowsSelectable = selectable;
    this.selectedIndex = 0;
    return true;
  }

  /** Safe writes can finish after the confirmation overlay closes. Their
   * verification/receipt page is a real focus state even when no menu request
   * is pending, but it is always read-only. */
  async presentLifecycle(
    title?: string,
    items: string[] = [],
    hint?: string,
  ): Promise<boolean> {
    if (!isActionLifecycleTitle(title)) return false;
    const transition = this.beginTransition();
    this.dataRowsSelectable = false;
    this.dataItemCount = 0;
    const shown = await this.hud.showList(items, { title, hint, selectable: false });
    if (!this.isCurrentTransition(transition)) return true;
    if (!shown) {
      await this.close();
      return true;
    }
    this.view = 'data';
    this.pendingTitle = 'ACTION';
    this.pendingRequestId = undefined;
    this.dataItemCount = items.length;
    this.dataRowsSelectable = false;
    this.selectedIndex = 0;
    return true;
  }

  /** Left double-tap. Closed: open the capability menu. Anywhere else: BACK
   *  home — one gesture, one meaning, matching the hint line. */
  async toggle(): Promise<void> {
    if (this.view === 'closed') {
      const transition = this.beginTransition();
      this.selectedIndex = 0;
      const shown = await this.hud.showSidebar([...CAPABILITIES], this.selectedIndex);
      if (!this.isCurrentTransition(transition)) return;
      if (shown) this.view = 'caps';
      else await this.close();
    } else {
      await this.close();
    }
  }

  /** Returns true when the event was consumed (a menu view is open). */
  handleList(list: ListEvent): boolean {
    if (this.view === 'closed' || this.view === 'data') return false;
    if (this.view === 'transition') return true;

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
    if (this.view === 'transition') return true;
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
    const rows = this.view === 'settings'
      ? POLICY_ROWS
      : this.view === 'more'
        ? MORE_CAPABILITIES
        : CAPABILITIES;
    this.selectedIndex = Math.max(0, Math.min(rows.length - 1, this.selectedIndex + delta));
    void this.hud.setSelection(this.selectedIndex);
    return true;
  }

  /** Right temple/ring click selects the highlighted custom-rendered row. */
  handleClick(): boolean {
    if (this.view === 'closed') return false;
    if (this.view === 'transition') return true;
    if (this.view === 'data') {
      if (!this.dataRowsSelectable || this.dataItemCount === 0) return true;
      const random = globalThis.crypto?.randomUUID?.()
        ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      const traceId = `hud-${random}`;
      const stage = this.pendingTitle === 'MESSAGES'
        ? 'message-selected'
        : 'capability-row-selected';
      console.info(`[joat trace] trace=${traceId} stage=${stage} index=${this.selectedIndex}`);
      this.link.send({
        kind: 'input',
        event: {
          containerName: 'capdata',
          selectedIndex: this.selectedIndex,
          selectedName: this.pendingTitle,
          traceId,
          requestId: this.pendingRequestId,
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

    const picked = this.view === 'more' ? MORE_CAPABILITIES[index] : CAPABILITIES[index];
    if (picked) {
      if (picked === 'More') {
        void this.openMore();
        return true;
      }
      // The data lives on the Mac; ask it to draw the view. The Mac answers
      // with hud.list / hud.text, which lands in the 'data' state where
      // double-tap still means back.
      void this.openCapability(picked, index);
    }
    return true;
  }

  private label(row: (typeof POLICY_ROWS)[number]): string {
    const active = row.policy === this.settings.resumePolicy;
    return `${active ? '*' : ' '} ${row.label}`;
  }

  private async openMore(): Promise<void> {
    const transition = this.beginTransition();
    this.selectedIndex = 0;
    const shown = await this.hud.showSidebar([...MORE_CAPABILITIES], this.selectedIndex);
    if (!this.isCurrentTransition(transition)) return;
    if (shown) this.view = 'more';
    else await this.close();
  }

  private async openCapability(
    picked: Exclude<(typeof CAPABILITIES)[number] | (typeof MORE_CAPABILITIES)[number], 'More'>,
    index: number,
  ): Promise<void> {
    const transition = this.beginTransition();
    const title = picked.toUpperCase();
    const requestId = `hud-request-${this.randomId()}`;
    const shown = await this.hud.showList([`Loading recent ${picked.toLowerCase()}…`], {
      title,
      hint: 'left x2 overview',
    });
    if (!this.isCurrentTransition(transition)) return;
    if (!shown) {
      await this.close();
      return;
    }
    this.view = 'data';
    this.pendingTitle = title;
    this.pendingRequestId = requestId;
    this.dataItemCount = 0;
    this.dataRowsSelectable = false;
    this.selectedIndex = 0;
    this.link.send({
      kind: 'input',
      event: {
        containerName: 'capmenu',
        eventType: 0,
        selectedIndex: index,
        selectedName: picked,
        requestId,
      },
    });
  }

  private randomId(): string {
    return globalThis.crypto?.randomUUID?.()
      ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  private beginTransition(): number {
    this.view = 'transition';
    this.transitionId += 1;
    return this.transitionId;
  }

  private isCurrentTransition(transition: number): boolean {
    return this.view === 'transition' && this.transitionId === transition;
  }

  private async close(): Promise<void> {
    this.transitionId += 1;
    this.view = 'closed';
    this.selectedIndex = 0;
    this.pendingTitle = undefined;
    this.pendingRequestId = undefined;
    this.dataItemCount = 0;
    this.dataRowsSelectable = false;
    // Restore a LIVE home, not a souvenir of the menu — a stale status line
    // over a blank page reads as a dead assistant (observed on hardware).
    await this.hud.showDefault();
    await this.hud.setStatus(this.micIsOpen() ? 'JOAT · listening' : 'JOAT · mic off');
    // HudPresenter restores the buffered dashboard atomically. Asking the Mac
    // to repaint here used to overwrite a newer card with an older last_reply.
  }
}
