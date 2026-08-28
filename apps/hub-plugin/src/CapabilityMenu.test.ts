import { describe, expect, it, vi } from 'vitest';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import { CapabilityMenu } from './CapabilityMenu';
import { Settings } from './Settings';
import type { HudPresenter } from './HudPresenter';
import type { MacLink } from './MacLink';

/**
 * The wearer's report from hardware: double-tap opened "a menu modal, but
 * not the one we designed", and there was no back gesture. These tests pin
 * the designed control-center: capability rows first, settings one level
 * down, and double-tap ALWAYS means back once anything is open.
 */

function rig(sidebarResult = true) {
  const sidebars: string[][] = [];
  const lists: string[][] = [];
  const listOptions: Array<{ title?: string; hint?: string } | undefined> = [];
  const statuses: string[] = [];
  const operations: string[] = [];
  const selections: number[] = [];
  const moves: number[] = [];
  const sent: Array<Record<string, unknown>> = [];
  let defaults = 0;
  const hud = {
    showSidebar: vi.fn(async (items: string[]) => {
      operations.push('sidebar');
      sidebars.push(items);
      return sidebarResult;
    }),
    showList: vi.fn(async (
      items: string[], options?: { title?: string; hint?: string },
    ) => {
      operations.push('list');
      lists.push(items);
      listOptions.push(options);
      return true;
    }),
    setStatus: vi.fn(async (t: string) => {
      operations.push('status');
      statuses.push(t);
      return true;
    }),
    setSelection: vi.fn(async (index: number) => {
      selections.push(index);
      return true;
    }),
    moveSelection: vi.fn(async (delta: number) => {
      moves.push(delta);
      return true;
    }),
    showDefault: vi.fn(async () => { defaults += 1; return true; }),
  } as unknown as HudPresenter;
  const link = { send: (m: Record<string, unknown>) => sent.push(m) } as unknown as MacLink;
  const menu = new CapabilityMenu(hud, new Settings(), link);
  return {
    menu, sidebars, lists, listOptions, operations, selections, moves, statuses, sent,
    defaultsCount: () => defaults,
  };
}

const click = (index: number) =>
  ({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: index }) as never;
const clickRow0 = () => ({ eventType: undefined, currentSelectItemIndex: undefined }) as never;

describe('CapabilityMenu', () => {
  it('double-tap from home opens the five capabilities in the transient sidebar', async () => {
    const { menu, sidebars, lists, operations } = rig();
    await menu.toggle();
    expect(sidebars[0]).toEqual(['Messages', 'Notifications', 'Calendar', 'Debate', 'Settings']);
    expect(lists).toEqual([]);
    expect(operations).toEqual(['sidebar']);
  });

  it('stays closed when the firmware rejects the sidebar rebuild', async () => {
    const { menu, sidebars } = rig(false);
    await menu.toggle();
    expect(sidebars).toHaveLength(1);
    expect(menu.isOpen()).toBe(false);
  });

  it('picking Messages removes the sidebar into a full-focus loading screen before asking the Mac', async () => {
    const { menu, lists, listOptions, operations, sent } = rig();
    await menu.toggle();
    expect(menu.handleList(clickRow0())).toBe(true); // protobuf zero-drop: row 0
    expect(lists[0]).toEqual(['Loading recent messages…']);
    expect(listOptions[0]).toEqual({ title: 'MESSAGES', hint: 'left x2 overview' });
    expect(operations).toEqual(['sidebar', 'list']);
    expect(sent[0]).toEqual({
      kind: 'input',
      event: { containerName: 'capmenu', eventType: 0, selectedIndex: 0, selectedName: 'Messages' },
    });
    // In the data view, list events are the Mac's business, not the menu's.
    expect(menu.handleList(click(1))).toBe(false);
  });

  it('moves the custom sidebar selection with text-capture scrolls and opens that row on click', async () => {
    const { menu, selections, lists, listOptions } = rig();
    await menu.toggle();
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(selections).toEqual([1]);
    expect(menu.handleClick()).toBe(true);
    expect(lists[0]).toEqual(['Loading recent notifications…']);
    expect(listOptions[0]?.title).toBe('NOTIFICATIONS');
  });

  it('keeps rows four and five reachable after the Mac replaces the loading view', async () => {
    const { menu, moves } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());

    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(moves).toEqual([1]);
  });

  it('double-tap is BACK from data — buffered home restores without an obsolete repaint', async () => {
    const { menu, sent, defaultsCount } = rig();
    await menu.toggle();
    menu.handleList(clickRow0()); // into Messages
    await menu.toggle();          // back
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
    expect(sent).toHaveLength(1); // only the capability selection request
  });

  it('rejects a delayed capability response after the wearer returns home', async () => {
    const { menu } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    expect(menu.acceptsList('MESSAGES')).toBe(true);
    await menu.toggle();
    expect(menu.acceptsList('MESSAGES')).toBe(false);
  });

  it('accepts only the newest capability response, including its setup suffix', async () => {
    const { menu } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await menu.toggle();
    await menu.toggle();
    menu.handleList(click(1));

    expect(menu.acceptsList('MESSAGES')).toBe(false);
    expect(menu.acceptsList('NOTIFICATIONS')).toBe(true);
    expect(menu.acceptsList('NOTIFICATIONS · SETUP')).toBe(true);
  });

  it('Settings lives one level down and still applies a policy', async () => {
    const { menu, lists, listOptions, sent } = rig();
    await menu.toggle();
    menu.handleList(click(4)); // Settings row
    expect(lists[0]!.some((r) => r.includes('Resume mic'))).toBe(true);
    expect(listOptions[0]).toEqual({
      title: 'LISTENING RESUME',
      hint: 'tap apply · left x2 overview',
    });
    menu.handleList(clickRow0()); // pick 'instantly' (row 0, zero-dropped)
    expect(sent.some((m) => m.kind === 'policy' && (m as { resume?: string }).resume === 'auto')).toBe(true);
    expect(menu.isOpen()).toBe(false); // applied and closed home
  });

  it('double-tap from the capability list is back, not deeper', async () => {
    const { menu, defaultsCount } = rig();
    await menu.toggle();
    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
  });
});
