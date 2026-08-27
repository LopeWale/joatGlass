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

function rig() {
  const lists: string[][] = [];
  const listOptions: Array<{ title?: string; hint?: string } | undefined> = [];
  const statuses: string[] = [];
  const operations: string[] = [];
  const sent: Array<Record<string, unknown>> = [];
  let defaults = 0;
  const hud = {
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
    showDefault: vi.fn(async () => { defaults += 1; return true; }),
  } as unknown as HudPresenter;
  const link = { send: (m: Record<string, unknown>) => sent.push(m) } as unknown as MacLink;
  const menu = new CapabilityMenu(hud, new Settings(), link);
  return { menu, lists, listOptions, operations, statuses, sent, defaultsCount: () => defaults };
}

const click = (index: number) =>
  ({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: index }) as never;
const clickRow0 = () => ({ eventType: undefined, currentSelectItemIndex: undefined }) as never;

describe('CapabilityMenu', () => {
  it('double-tap from home opens the DESIGNED menu: the five capabilities', () => {
    const { menu, lists, listOptions, operations } = rig();
    menu.toggle();
    expect(lists[0]).toEqual(['Messages', 'Notifications', 'Calendar', 'Debate', 'Settings']);
    expect(listOptions[0]).toEqual({
      title: 'CAPABILITIES',
      hint: 'tap open · left x2 back',
    });
    expect(operations.slice(0, 2)).toEqual(['status', 'list']);
  });

  it('picking Messages asks the Mac for the data view and enters data state', () => {
    const { menu, sent } = rig();
    menu.toggle();
    expect(menu.handleList(clickRow0())).toBe(true); // protobuf zero-drop: row 0
    expect(sent[0]).toEqual({
      kind: 'input',
      event: { containerName: 'capmenu', eventType: 0, selectedIndex: 0, selectedName: 'Messages' },
    });
    // In the data view, list events are the Mac's business, not the menu's.
    expect(menu.handleList(click(1))).toBe(false);
  });

  it('double-tap is BACK from data — buffered home restores without an obsolete repaint', async () => {
    const { menu, sent, defaultsCount } = rig();
    menu.toggle();
    menu.handleList(clickRow0()); // into Messages
    menu.toggle();                // back
    await Promise.resolve();      // close waits for the live status paint first
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
    expect(sent).toHaveLength(1); // only the capability selection request
  });

  it('Settings lives one level down and still applies a policy', () => {
    const { menu, lists, listOptions, sent } = rig();
    menu.toggle();
    menu.handleList(click(4)); // Settings row
    expect(lists[1]!.some((r) => r.includes('Resume mic'))).toBe(true);
    expect(listOptions[1]).toEqual({
      title: 'LISTENING RESUME',
      hint: 'tap apply · left x2 back',
    });
    menu.handleList(clickRow0()); // pick 'instantly' (row 0, zero-dropped)
    expect(sent.some((m) => m.kind === 'policy' && (m as { resume?: string }).resume === 'auto')).toBe(true);
    expect(menu.isOpen()).toBe(false); // applied and closed home
  });

  it('double-tap from the capability list is back, not deeper', async () => {
    const { menu, defaultsCount } = rig();
    menu.toggle();
    menu.toggle();
    await Promise.resolve();
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
  });
});
