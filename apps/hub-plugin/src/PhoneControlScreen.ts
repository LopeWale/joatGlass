import {
  accountBadge,
  isPhoneTab,
  loginStep,
  PHONE_TABS,
  preferredTab,
  profileActions,
  profileSubtitle,
  providerTitle,
  roomBadge,
  seatDetail,
  seatFix,
  type Badge,
  type PhoneTab,
  type ProfileAction,
} from './PhoneControlModel';
import type {
  ControlActionMessage,
  ControlLoginView,
  ControlModelSlotKey,
  ControlProviderView,
  ControlStateView,
} from './types';

const TAB_KEY = 'joat.phone.tab';
const MODEL_SLOTS: readonly ControlModelSlotKey[] = ['codex', 'claude', 'assistant'];

interface PhoneControlScreenOptions {
  send: (message: ControlActionMessage) => void;
}

/**
 * The phone side of the plugin: tabs for Brainstorm, Accounts and
 * Connection. Renders the Mac's whole `control.state` on every message —
 * the screen is small and human-paced, so a full redraw is simpler and
 * cannot drift. All Mac-supplied text goes in through textContent.
 */
export class PhoneControlScreen {
  private state: ControlStateView | undefined;
  private connected = false;
  private tab: PhoneTab = 'brainstorm';
  /** Two-tap confirmation for destructive buttons (no window.confirm in WebViews). */
  private armed: string | undefined;
  private addingFor: ControlProviderView['provider'] | undefined;
  private focusAdd = false;

  constructor(private readonly options: PhoneControlScreenOptions) {
    try {
      const saved = localStorage.getItem(TAB_KEY);
      if (isPhoneTab(saved)) this.tab = saved;
    } catch {
      // Storage can be unavailable in a WebView; the default tab is fine.
    }
    for (const tab of PHONE_TABS) {
      document.getElementById(`tab-${tab}`)?.addEventListener('click', () => this.selectTab(tab));
    }
    document.getElementById('c-reset')?.addEventListener('click', () => {
      if (this.confirmTap('reset')) this.send({ kind: 'control.action', action: 'brainstorm.reset' });
    });
    document.getElementById('c-refresh')?.addEventListener('click', () => {
      this.send({ kind: 'control.action', action: 'refresh' });
    });
    this.selectTab(this.tab);
    this.render();
  }

  setConnected(connected: boolean): void {
    if (this.connected === connected) return;
    this.connected = connected;
    this.render();
  }

  update(state: ControlStateView): void {
    const previous = this.state;
    this.state = state;
    const next = preferredTab(state, this.tab);
    // Jump to Accounts once, when a sign-in starts, never repeatedly.
    if (next !== this.tab && previous?.login?.stage !== state.login?.stage) this.selectTab(next);
    this.render();
  }

  // ── tabs ──────────────────────────────────────────────────────────────────

  private selectTab(tab: PhoneTab): void {
    this.tab = tab;
    for (const candidate of PHONE_TABS) {
      const button = document.getElementById(`tab-${candidate}`);
      const view = document.getElementById(`view-${candidate}`);
      button?.setAttribute('aria-selected', String(candidate === tab));
      button?.classList.toggle('selected', candidate === tab);
      if (view) view.hidden = candidate !== tab;
    }
    try {
      localStorage.setItem(TAB_KEY, tab);
    } catch {
      // Remembering the tab is a convenience only.
    }
  }

  // ── rendering ─────────────────────────────────────────────────────────────

  private render(): void {
    // A full redraw must not eat what the wearer is typing: a Brainstorm
    // phase change mid-paste would otherwise clear the sign-in code field.
    const kept = new Map<string, string>();
    let focused: string | undefined;
    for (const input of document.querySelectorAll<HTMLInputElement>('input[data-keep]')) {
      const key = input.dataset.keep ?? '';
      kept.set(key, input.value);
      if (document.activeElement === input) focused = key;
    }
    this.draw();
    for (const input of document.querySelectorAll<HTMLInputElement>('input[data-keep]')) {
      const key = input.dataset.keep ?? '';
      const value = kept.get(key);
      if (value !== undefined) input.value = value;
      if (key === focused || (this.focusAdd && key.startsWith('add-'))) input.focus();
    }
    this.focusAdd = false;
  }

  private draw(): void {
    this.write('c-notice', this.state?.notice ?? '');
    this.renderRoom();
    this.renderSeats();
    this.renderModels();
    this.renderLogin();
    this.renderAccounts();
    const live = this.connected && this.state !== undefined;
    for (const id of ['c-reset', 'c-refresh']) {
      const button = document.getElementById(id) as HTMLButtonElement | null;
      if (button) button.disabled = !live;
    }
    const reset = document.getElementById('c-reset');
    if (reset) reset.textContent = this.armed === 'reset' ? 'Tap again to reset' : 'Reset Brainstorm';
  }

  private renderRoom(): void {
    const host = document.getElementById('c-room');
    if (!host) return;
    host.replaceChildren();
    if (!this.connected || !this.state) {
      host.append(el('p', 'support', this.connected
        ? 'Loading from your Mac…'
        : 'Connect to your Mac (Connection tab) to manage Brainstorm.'));
      return;
    }
    const { brainstorm } = this.state;
    const head = el('div', 'room-head');
    head.append(el('span', 'room-title', 'Brainstorm room'),
      badge(roomBadge(brainstorm.state, brainstorm.phase)));
    host.append(head, el('p', 'room-detail', brainstorm.detail));
  }

  private renderSeats(): void {
    const host = document.getElementById('c-seats');
    if (!host) return;
    host.replaceChildren();
    for (const seat of this.state?.brainstorm.seats ?? []) {
      const row = el('button', 'row seat');
      (row as HTMLButtonElement).type = 'button';
      const who = el('span', 'who');
      who.append(el('span', 'name', seat.title), el('span', 'sub', seatDetail(seat)));
      const fix = seatFix(seat);
      if (fix) who.append(el('span', 'fix', fix));
      row.append(who, badge(seat.installed ? accountBadge(seat.account) : accountBadge('missing')));
      row.addEventListener('click', () => this.selectTab('accounts'));
      host.append(row);
    }
    host.hidden = host.childElementCount === 0;
  }

  private renderModels(): void {
    const host = document.getElementById('c-models');
    if (!host) return;
    host.replaceChildren();
    for (const slot of this.state?.models ?? []) {
      if (!MODEL_SLOTS.includes(slot.key as ControlModelSlotKey)) continue;
      const row = el('label', 'row');
      const select = document.createElement('select');
      select.disabled = !this.connected;
      for (const option of slot.options) {
        const item = document.createElement('option');
        item.value = option;
        item.textContent = option;
        select.append(item);
      }
      select.value = slot.current;
      select.addEventListener('change', () => this.send({
        kind: 'control.action', action: 'model.select',
        slot: slot.key as ControlModelSlotKey, option: select.value,
      }));
      row.append(el('span', 'name', slot.title), select);
      host.append(row);
    }
    host.hidden = host.childElementCount === 0;
  }

  private renderLogin(): void {
    const host = document.getElementById('c-login');
    if (!host) return;
    host.replaceChildren();
    const login = this.state?.login;
    if (!login || !this.state) return;
    host.append(this.loginCard(login, providerTitle(this.state, login.provider)));
  }

  private loginCard(login: ControlLoginView, title: string): HTMLElement {
    const step = loginStep(login, title);
    const card = el('div', `card login ${login.stage}`);
    card.append(el('h3', 'login-title', step.title));
    if (login.message) card.append(el('p', 'support', login.message));
    if (step.showLink && login.url) {
      const open = el('a', 'btn primary', 'Open sign-in page') as HTMLAnchorElement;
      open.href = login.url;
      open.target = '_blank';
      open.rel = 'noopener noreferrer';
      const copy = el('button', 'btn ghost', 'Copy link') as HTMLButtonElement;
      copy.type = 'button';
      copy.addEventListener('click', () => void copyText(login.url ?? '', copy));
      const buttons = el('div', 'buttons');
      buttons.append(open, copy);
      card.append(buttons);
    }
    if (step.codeToType) {
      const code = el('div', 'device-code');
      const value = el('span', 'code', step.codeToType);
      const copy = el('button', 'btn ghost', 'Copy code') as HTMLButtonElement;
      copy.type = 'button';
      copy.addEventListener('click', () => void copyText(step.codeToType ?? '', copy));
      code.append(el('span', 'label', 'Code'), value, copy);
      card.append(code);
    }
    if (step.askForCode) {
      const form = document.createElement('form');
      form.className = 'paste';
      const input = document.createElement('input');
      input.type = 'text';
      input.autocomplete = 'one-time-code';
      input.autocapitalize = 'off';
      input.spellcheck = false;
      input.placeholder = 'Paste the code from the sign-in page';
      input.dataset.keep = 'paste-code';
      input.setAttribute('aria-label', 'Sign-in code');
      const submit = el('button', 'btn primary', 'Submit code') as HTMLButtonElement;
      submit.type = 'submit';
      form.append(input, submit);
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        const code = input.value.trim();
        if (!code) return;
        input.value = '';
        this.send({ kind: 'control.action', action: 'account.code', code });
      });
      card.append(form);
    }
    const close = el('button', 'btn ghost', step.finished ? 'Done' : 'Cancel sign-in') as HTMLButtonElement;
    close.type = 'button';
    close.addEventListener('click', () => this.send({
      kind: 'control.action', action: step.finished ? 'account.dismiss' : 'account.cancel',
    }));
    card.append(close);
    return card;
  }

  private renderAccounts(): void {
    const host = document.getElementById('c-accounts');
    if (!host) return;
    host.replaceChildren();
    if (!this.connected || !this.state) {
      host.append(el('p', 'support', 'Connect to your Mac to see its Claude and Codex accounts.'));
      return;
    }
    for (const provider of this.state.accounts) host.append(this.providerSection(provider));
    host.append(el('p', 'support',
      'Each account keeps its own login on the Mac, so switching takes effect on the next turn. '
      + 'JOAT only chooses which login the official CLI uses — it never sees your password or tokens.'));
  }

  private providerSection(provider: ControlProviderView): HTMLElement {
    const section = el('section', 'provider');
    section.append(el('h2', '', provider.title));
    const group = el('div', 'group');
    if (!provider.installed) {
      group.append(el('p', 'row support', `${provider.title} CLI isn't installed on the Mac.`));
    }
    for (const profile of provider.profiles) {
      const item = el('div', `profile${profile.active ? ' active' : ''}`);
      const top = el('div', 'row');
      const who = el('span', 'who');
      const name = el('span', 'name', profile.label);
      if (profile.active) name.append(el('span', 'in-use', 'In use'));
      who.append(name);
      const sub = profileSubtitle(profile);
      if (sub) who.append(el('span', 'sub', sub));
      top.append(who, badge(accountBadge(profile.state)));
      item.append(top);
      const actions = profileActions(profile, provider.installed);
      if (actions.length) {
        const buttons = el('div', 'buttons compact');
        for (const action of actions) buttons.append(this.profileButton(provider, profile.id, action));
        item.append(buttons);
      }
      group.append(item);
    }
    group.append(this.addAccount(provider));
    section.append(group);
    return section;
  }

  private profileButton(
    provider: ControlProviderView, profileId: string, action: ProfileAction,
  ): HTMLButtonElement {
    const key = `${action.action}:${provider.provider}:${profileId}`;
    const armed = this.armed === key;
    const button = el('button', `btn${action.primary ? ' primary' : ' ghost'}${action.destructive ? ' danger' : ''}`,
      armed ? `Tap again to ${action.label.toLowerCase()}` : action.label) as HTMLButtonElement;
    button.type = 'button';
    button.disabled = !this.connected;
    button.addEventListener('click', () => {
      if (action.destructive && !this.confirmTap(key)) return;
      this.send({
        kind: 'control.action', action: action.action, provider: provider.provider, profileId,
      });
    });
    return button;
  }

  private addAccount(provider: ControlProviderView): HTMLElement {
    const wrap = el('div', 'add');
    if (!provider.installed) return wrap;
    if (this.addingFor !== provider.provider) {
      const open = el('button', 'action', `Add ${provider.title} account`) as HTMLButtonElement;
      open.type = 'button';
      open.disabled = !this.connected;
      open.addEventListener('click', () => {
        this.addingFor = provider.provider;
        this.focusAdd = true;
        this.render();
      });
      wrap.append(open);
      return wrap;
    }
    const form = document.createElement('form');
    form.className = 'paste';
    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 40;
    input.placeholder = 'Name, e.g. Work or Personal';
    input.dataset.keep = `add-${provider.provider}`;
    input.setAttribute('aria-label', `${provider.title} account name`);
    const submit = el('button', 'btn primary', 'Add and sign in') as HTMLButtonElement;
    submit.type = 'submit';
    const cancel = el('button', 'btn ghost', 'Cancel') as HTMLButtonElement;
    cancel.type = 'button';
    cancel.addEventListener('click', () => {
      this.addingFor = undefined;
      this.render();
    });
    form.append(input, submit, cancel);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const label = input.value.trim();
      if (!label) return;
      this.addingFor = undefined;
      this.send({ kind: 'control.action', action: 'account.add', provider: provider.provider, label });
    });
    wrap.append(form);
    return wrap;
  }

  // ── helpers ───────────────────────────────────────────────────────────────

  private confirmTap(key: string): boolean {
    if (this.armed === key) {
      this.armed = undefined;
      this.render();
      return true;
    }
    this.armed = key;
    this.render();
    setTimeout(() => {
      if (this.armed === key) {
        this.armed = undefined;
        this.render();
      }
    }, 4000);
    return false;
  }

  private send(message: ControlActionMessage): void {
    if (!this.connected) return;
    this.options.send(message);
  }

  private write(id: string, text: string): void {
    const node = document.getElementById(id);
    if (node) node.textContent = text;
  }
}

function el(tag: string, className: string, text?: string): HTMLElement {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function badge(value: Badge): HTMLElement {
  return el('span', `badge ${value.tone}`, value.text);
}

async function copyText(text: string, button: HTMLButtonElement): Promise<void> {
  const original = button.textContent;
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch {
    // The Even WebView may refuse the async clipboard; fall back to a
    // selected field, which the platform copy command can read.
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.position = 'fixed';
    field.style.opacity = '0';
    document.body.append(field);
    field.select();
    try {
      copied = document.execCommand('copy');
    } catch {
      copied = false;
    }
    field.remove();
  }
  button.textContent = copied ? 'Copied' : 'Copy failed — press and hold to copy';
  setTimeout(() => { button.textContent = original; }, 2000);
}
