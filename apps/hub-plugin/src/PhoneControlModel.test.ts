import { describe, expect, it } from 'vitest';
import {
  accountBadge,
  loginStep,
  preferredTab,
  profileActions,
  roomBadge,
  seatDetail,
  seatFix,
} from './PhoneControlModel';
import type { ControlProfileView, ControlStateView } from './types';

const profile = (over: Partial<ControlProfileView> = {}): ControlProfileView => ({
  id: 'acct-0a1b2c3d', label: 'Work', state: 'logged_in', isDefault: false, active: false, ...over,
});

const state = (over: Partial<ControlStateView> = {}): ControlStateView => ({
  revision: 1,
  brainstorm: { state: 'ready', phase: 'idle', detail: '', seats: [] },
  accounts: [],
  models: [],
  login: null,
  notice: '',
  ...over,
});

describe('PhoneControlModel', () => {
  it('names every account state in words, never the wire code', () => {
    expect(accountBadge('logged_in')).toEqual({ text: 'Signed in', tone: 'good' });
    expect(accountBadge('logged_out').tone).toBe('bad');
    expect(accountBadge('limited').text).toBe('Usage limit');
    expect(accountBadge('anything-new').text).toBe('Checking…');
  });

  it('describes a running room by phase', () => {
    expect(roomBadge('running', 'cross_review').text).toBe('Running · cross review');
    expect(roomBadge('unavailable', 'idle').tone).toBe('bad');
  });

  it('tells the wearer the next step for a stuck seat', () => {
    const seat = {
      name: 'claude', title: 'Claude', installed: true, account: 'limited',
      accountLabel: 'Work', model: 'default',
    };
    expect(seatDetail(seat)).toBe('Work · model default');
    expect(seatFix(seat)).toMatch(/Switch to another account/);
    expect(seatFix({ ...seat, installed: false })).toMatch(/Install/);
    expect(seatFix({ ...seat, account: 'logged_in' })).toBe('');
  });

  it('offers sign-in first for a signed-out account and never removes the default', () => {
    const out = profileActions(profile({ state: 'logged_out' }), true);
    expect(out[0]).toMatchObject({ action: 'account.signin', primary: true });
    const defaults = profileActions(profile({ isDefault: true, active: true }), true);
    expect(defaults.map((a) => a.action)).toEqual(['account.signout']);
    const spare = profileActions(profile(), true).map((a) => a.action);
    expect(spare).toEqual(['account.activate', 'account.signout', 'account.remove']);
    expect(profileActions(profile(), false)).toEqual([]);
  });

  it('shows the Codex code to type and asks Claude for the pasted one', () => {
    const codex = loginStep({
      provider: 'codex', profileId: 'default', stage: 'open_link', message: '', needsCode: false,
      url: 'https://auth.openai.com/codex/device', code: 'GBEA-VS5GL',
    }, 'Codex');
    expect(codex).toMatchObject({ showLink: true, codeToType: 'GBEA-VS5GL', askForCode: false, finished: false });
    const claude = loginStep({
      provider: 'claude', profileId: 'default', stage: 'enter_code', message: '', needsCode: true,
      url: 'https://claude.com/cai/oauth/authorize',
    }, 'Claude');
    expect(claude.askForCode).toBe(true);
    const done = loginStep({ provider: 'claude', profileId: 'default', stage: 'done', message: 'Signed in.', needsCode: false }, 'Claude');
    expect(done).toMatchObject({ finished: true, showLink: false, title: 'Claude signed in' });
  });

  it('jumps to Accounts only while a sign-in is in flight', () => {
    const live = state({
      login: { provider: 'codex', profileId: 'default', stage: 'open_link', message: '', needsCode: false },
    });
    expect(preferredTab(live, 'brainstorm')).toBe('accounts');
    expect(preferredTab(state(), 'brainstorm')).toBe('brainstorm');
  });
});
