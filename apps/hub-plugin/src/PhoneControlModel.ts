import type {
  ControlAccountState,
  ControlLoginView,
  ControlProfileView,
  ControlProviderName,
  ControlSeatView,
  ControlStateView,
} from './types';

/**
 * Pure view logic for the phone's control screen, kept apart from the DOM so
 * it is testable without a browser. Every string the wearer reads on the
 * phone is decided here.
 */

export type PhoneTab = 'brainstorm' | 'accounts' | 'connection';
export const PHONE_TABS: readonly PhoneTab[] = ['brainstorm', 'accounts', 'connection'];
export const PHONE_TAB_TITLES: Record<PhoneTab, string> = {
  brainstorm: 'Brainstorm',
  accounts: 'Accounts',
  connection: 'Connection',
};

export type BadgeTone = 'good' | 'warn' | 'bad' | 'quiet';

export interface Badge {
  text: string;
  tone: BadgeTone;
}

export function accountBadge(state: ControlAccountState): Badge {
  switch (state) {
    case 'logged_in': return { text: 'Signed in', tone: 'good' };
    case 'logged_out': return { text: 'Signed out', tone: 'bad' };
    case 'limited': return { text: 'Usage limit', tone: 'warn' };
    case 'missing': return { text: 'Not installed', tone: 'bad' };
    default: return { text: 'Checking…', tone: 'quiet' };
  }
}

export function roomBadge(state: string, phase: string): Badge {
  if (state === 'running') {
    const step = PHASE_TITLES[phase];
    return { text: step ? `Running · ${step}` : 'Running', tone: 'good' };
  }
  if (state === 'unavailable') return { text: 'Unavailable', tone: 'bad' };
  if (state === 'failed') return { text: 'Last run failed', tone: 'warn' };
  return { text: 'Ready', tone: 'good' };
}

const PHASE_TITLES: Record<string, string> = {
  opening: 'opening',
  independent: 'first answers',
  cross_review: 'cross review',
  meta_review: 'judging',
  synthesis: 'summary',
  closing: 'wrapping up',
  turn: 'speaking',
};

/** What a seat row says under its name: account and model, plainly. */
export function seatDetail(seat: ControlSeatView): string {
  if (!seat.installed) return `${seat.title} CLI isn't installed on the Mac`;
  const parts = [seat.accountLabel, seat.model ? `model ${seat.model}` : ''].filter(Boolean);
  return parts.join(' · ');
}

/** The one thing to do next for a seat that cannot speak, if any. */
export function seatFix(seat: ControlSeatView): string {
  if (!seat.installed) return 'Install or update the app that provides it on the Mac.';
  if (seat.account === 'logged_out') return 'Sign in, or switch to another account.';
  if (seat.account === 'limited') return 'Switch to another account, or wait for the limit to reset.';
  return '';
}

export interface ProfileAction {
  label: string;
  action: 'account.activate' | 'account.signin' | 'account.signout' | 'account.remove';
  primary: boolean;
  destructive: boolean;
}

/** The buttons for one saved account, primary action first. */
export function profileActions(profile: ControlProfileView, providerInstalled: boolean): ProfileAction[] {
  if (!providerInstalled) return [];
  const actions: ProfileAction[] = [];
  if (profile.state === 'logged_out' || profile.state === 'unknown') {
    actions.push({ label: 'Sign in', action: 'account.signin', primary: true, destructive: false });
  }
  if (!profile.active && profile.state !== 'missing') {
    actions.push({
      label: 'Use this account', action: 'account.activate',
      primary: actions.length === 0, destructive: false,
    });
  }
  if (profile.state === 'logged_in' || profile.state === 'limited') {
    actions.push({ label: 'Sign out', action: 'account.signout', primary: false, destructive: true });
  }
  if (!profile.isDefault) {
    actions.push({ label: 'Remove', action: 'account.remove', primary: false, destructive: true });
  }
  return actions;
}

export function profileSubtitle(profile: ControlProfileView): string {
  const who = profile.email ?? (profile.isDefault ? 'The login already on this Mac' : '');
  return [who, profile.plan].filter(Boolean).join(' · ');
}

export interface LoginStep {
  title: string;
  /** Show the sign-in link as the primary action. */
  showLink: boolean;
  /** Codex: the code to type on the website. */
  codeToType?: string;
  /** Claude: ask for the code the website shows. */
  askForCode: boolean;
  finished: boolean;
}

export function loginStep(login: ControlLoginView, providerTitle: string): LoginStep {
  const finished = login.stage === 'done' || login.stage === 'failed' || login.stage === 'cancelled';
  const title = login.stage === 'done' ? `${providerTitle} signed in`
    : login.stage === 'failed' ? `${providerTitle} sign-in didn't finish`
      : login.stage === 'cancelled' ? 'Sign-in cancelled'
        : `Sign in to ${providerTitle}`;
  return {
    title,
    showLink: !finished && Boolean(login.url),
    codeToType: !finished ? login.code : undefined,
    askForCode: !finished && login.needsCode,
    finished,
  };
}

export function providerTitle(state: ControlStateView, provider: ControlProviderName): string {
  return state.accounts.find((a) => a.provider === provider)?.title
    ?? (provider === 'claude' ? 'Claude' : 'Codex');
}

/** The tab the screen opens on: Accounts while a sign-in is in flight. */
export function preferredTab(state: ControlStateView, current: PhoneTab): PhoneTab {
  if (state.login && !['done', 'failed', 'cancelled'].includes(state.login.stage)) return 'accounts';
  return current;
}

export function isPhoneTab(value: unknown): value is PhoneTab {
  return typeof value === 'string' && (PHONE_TABS as readonly string[]).includes(value);
}
