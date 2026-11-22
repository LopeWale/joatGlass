import { afterEach, describe, expect, it, vi } from 'vitest';
import indexHtml from '../index.html?raw';
import type { LinkState } from './MacLink';
import {
  PhoneControlPanel,
  brainLabel,
  isPhoneBrain,
} from './PhoneControlPanel';

const PHONE = 'ws://127.0.0.1:8787/glasses';
const MAC_A = 'wss://mac-a.example.ts.net/glasses';
const MAC_B = 'wss://mac-b.example.ts.net/glasses';

class FakeElement {
  id = '';
  textContent = '';
  value = '';
  disabled = false;
  hidden = false;
  readonly children: FakeElement[] = [];
  private readonly listeners = new Map<string, Array<() => void>>();

  append(child: FakeElement): void {
    this.children.push(child);
    if (!this.value && child.value) this.value = child.value;
  }

  addEventListener(type: string, listener: () => void): void {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  dispatch(type: string): void {
    for (const listener of this.listeners.get(type) ?? []) listener();
  }
}

function installFakeDom() {
  const ids = [
    'p-brain',
    'p-mac-pinned-row',
    'p-mac-pinned',
    'p-connect',
    'p-resume',
    'p-location-check',
    'p-location-result',
    'p-link',
    'p-url',
    'p-active-brain',
    'p-recovery',
    'p-action',
  ] as const;
  const elements = new Map<string, FakeElement>();
  for (const id of ids) {
    const el = new FakeElement();
    el.id = id;
    if (id === 'p-mac-pinned-row') el.hidden = true;
    elements.set(id, el);
  }
  (globalThis as { document?: unknown }).document = {
    getElementById: (id: string) => elements.get(id) ?? null,
    querySelector: (selector: string) => (
      selector.startsWith('#') ? (elements.get(selector.slice(1)) ?? null) : null
    ),
    createElement: () => new FakeElement(),
  };
  return elements;
}

afterEach(() => {
  delete (globalThis as { document?: unknown }).document;
  delete (globalThis as { localStorage?: unknown }).localStorage;
});

describe('isPhoneBrain & brainLabel', () => {
  it('identifies loopback JOAT Brain URLs and formats clean labels', () => {
    expect(isPhoneBrain(PHONE)).toBe(true);
    expect(isPhoneBrain('ws://localhost:8787/glasses')).toBe(true);
    expect(isPhoneBrain(MAC_A)).toBe(false);
    expect(brainLabel(PHONE)).toBe('This iPhone · JOAT Brain');
    expect(brainLabel(MAC_A)).toBe('mac-a · Tailscale (secure)');
    expect(brainLabel('ws://192.168.1.25:8787/glasses')).toBe('192.168.1.25 · Local network');
  });
});

describe('index.html markup contract', () => {
  it('preserves all existing control IDs and includes the pinned Mac row', () => {
    const html = indexHtml;
    for (const id of [
      'panel',
      'p-link',
      'view-connection',
      'connection-title',
      'p-brain',
      'p-mac-pinned-row',
      'p-mac-pinned',
      'p-connect',
      'brain-help',
      'p-action',
      'voice-title',
      'p-resume',
      'p-active-brain',
      'p-url',
      'p-status',
      'p-mic',
      'p-policy',
      'p-recovery',
      'p-location-check',
      'p-location-result',
    ]) {
      expect(html).toContain(`id="${id}"`);
    }
    expect(html).toContain('Primary connection');
    expect(html).toContain('Messages &amp; Agent Terminal');
  });
});

describe('PhoneControlPanel split-brain UI', () => {
  it('shows the pinned Mac row and clear split-brain copy when connected to the iPhone brain', () => {
    const els = installFakeDom();
    const link = { activeUrl: PHONE, linkState: 'connected' as LinkState, wasReplaced: false };
    const handoff: { active: boolean; capability?: string; homeUrl?: string; lastMacUrl?: string | null } = {
      active: false,
      lastMacUrl: null,
    };
    const panel = new PhoneControlPanel({
      link,
      urls: [PHONE, MAC_A, MAC_B],
      settings: { resumePolicy: 'confirm' },
      handoff,
      changePolicy: vi.fn(),
      checkLocation: vi.fn(async () => null),
      canReconnect: () => true,
      selectBrain: vi.fn(async () => true),
    });

    expect(els.get('p-mac-pinned-row')!.hidden).toBe(false);
    expect(els.get('p-mac-pinned')!.textContent).toBe('mac-a · Tailscale (secure) (always)');
    expect(els.get('p-active-brain')!.textContent).toBe('This iPhone · JOAT Brain');
    expect(els.get('p-recovery')!.textContent).toBe(
      'JOAT Brain is active on this iPhone. Messages & Agent Terminal stay connected to mac-a · Tailscale (secure).',
    );
    expect(els.get('p-action')!.textContent).toBe('');

    // During an active Mac handoff, keeps #p-brain synced to homeUrl and explains 2x tap return!
    handoff.active = true;
    handoff.capability = 'Messages';
    handoff.homeUrl = PHONE;
    handoff.lastMacUrl = MAC_B;
    link.activeUrl = MAC_B;
    panel.updateLink('connected');

    expect(els.get('p-brain')!.value).toBe(PHONE);
    expect(els.get('p-mac-pinned')!.textContent).toBe('mac-b · Tailscale (secure) (always)');
    expect(els.get('p-active-brain')!.textContent).toBe('mac-b · Tailscale (secure)');
    expect(els.get('p-action')!.textContent).toBe(
      'Messages is active on mac-b · Tailscale (secure). 2× tap back to Home returns to This iPhone · JOAT Brain.',
    );
    expect(els.get('p-recovery')!.textContent).toBe(
      'Messages is active on mac-b · Tailscale (secure). 2× tap back to Home returns to This iPhone · JOAT Brain.',
    );

    // If the user manually picks MAC_A in the dropdown during handoff, shows the manual switch prompt.
    els.get('p-brain')!.value = MAC_A;
    els.get('p-brain')!.dispatch('change');
    expect(els.get('p-action')!.textContent).toContain('Tap Connect to switch to mac-a · Tailscale (secure)');
  });

  it('hides the pinned Mac row when no split phone+Mac pair is configured', () => {
    const els = installFakeDom();
    new PhoneControlPanel({
      link: { activeUrl: MAC_A, linkState: 'connected' as LinkState, wasReplaced: false },
      urls: [MAC_A, MAC_B],
      settings: { resumePolicy: 'confirm' },
      changePolicy: vi.fn(),
      checkLocation: vi.fn(async () => null),
      canReconnect: () => true,
      selectBrain: vi.fn(async () => true),
    });
    expect(els.get('p-mac-pinned-row')!.hidden).toBe(true);
    expect(els.get('p-mac-pinned')!.textContent).toBe('');
  });
});
