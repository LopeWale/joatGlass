import { beforeEach, describe, expect, it, vi } from 'vitest';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import { SettingsMenu } from './SettingsMenu';
import { Settings } from './Settings';
import type { HudPresenter } from './HudPresenter';
import type { MacLink } from './MacLink';
import type { UplinkMessage } from './types';

function make() {
  const sent: UplinkMessage[] = [];
  const hud = {
    showList: vi.fn(async (_items: string[]) => true),
    setStatus: vi.fn(async (_text: string) => true),
    showDefault: vi.fn(async () => true),
  };
  const settings = new Settings();
  const menu = new SettingsMenu(
    hud as unknown as HudPresenter,
    settings,
    { send: (m: UplinkMessage) => sent.push(m) } as unknown as MacLink,
  );
  return { menu, hud, settings, sent };
}

beforeEach(() => {
  delete (globalThis as { localStorage?: unknown }).localStorage;
});

describe('SettingsMenu', () => {
  it('closed: consumes nothing', () => {
    const { menu } = make();
    expect(menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: 0 } as never)).toBe(false);
  });

  it('toggle renders the list with the active policy marked', () => {
    const { menu, hud } = make(); // default policy: confirm → third row
    menu.toggle();
    expect(menu.isOpen()).toBe(true);
    const rows = hud.showList.mock.calls[0]?.[0] ?? [];
    expect(rows).toHaveLength(3);
    expect(rows[2]?.startsWith('*')).toBe(true);
    expect(rows[0]?.startsWith('*')).toBe(false);
  });

  it('row click applies + persists + announces + closes', () => {
    const { menu, settings, sent } = make();
    menu.toggle();
    const consumed = menu.handleList({
      eventType: OsEventTypeList.CLICK_EVENT,
      currentSelectItemIndex: 0,
    } as never);
    expect(consumed).toBe(true);
    expect(settings.resumePolicy).toBe('auto');
    expect(sent).toEqual([{ kind: 'policy', resume: 'auto' }]);
    expect(menu.isOpen()).toBe(false);
  });

  it('scrolls and out-of-range clicks are consumed without applying anything', () => {
    const { menu, settings, sent } = make();
    menu.toggle();
    expect(menu.handleList({ eventType: OsEventTypeList.SCROLL_TOP_EVENT } as never)).toBe(true);
    expect(menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: 99 } as never)).toBe(true);
    expect(settings.resumePolicy).toBe('confirm');
    expect(sent).toEqual([]);
    expect(menu.isOpen()).toBe(true);
  });

  it('toggle twice backs out via the default layout without changes', () => {
    const { menu, hud, sent } = make();
    menu.toggle();
    menu.toggle();
    expect(menu.isOpen()).toBe(false);
    expect(hud.showDefault).toHaveBeenCalledTimes(1);
    expect(sent).toEqual([]);
  });
});
