import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Chrome } from './Chrome';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import type { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { hudSpineLabel } from './HudCanvasRenderer';
import type { HudVisualFrame } from './HudVisualSurface';
import { ContainerId } from './types';

// These tests pin the IMAGE overview (VITE_NATIVE_OVERVIEW=0). The text
// overview, which is Home by default, has its own: HudPresenter.textOverview.test.ts.
setNativeOverviewForTest(false);

/**
 * This file did not exist while Chrome shipped three regressions, which is
 * precisely why they shipped green: nothing in the suite could observe
 * Chrome interacting with a real HudPresenter.
 *
 * The one that reached the wearer: Chrome paints the status bar AND the
 * dashboard hint on every tick, including while a focus page (menu, message
 * list) is up. Those writes must never reach a container the focus layout
 * does not declare, because a failed paint triggers the self-heal, which
 * rebuilds the page and destroys the menu — "the menu shows but immediately
 * disappears and the screen goes black".
 */

const paints: TextContainerUpgrade[] = [];
let rebuilds = 0;

function realPresenter(): HudPresenter {
  const client = {
    require: () => ({
      textContainerUpgrade: async (u: TextContainerUpgrade) => {
        paints.push(u);
        return true;
      },
      rebuildPageContainer: async () => {
        rebuilds += 1;
        return true;
      },
    }),
  } as unknown as BridgeClient;
  return new HudPresenter(client, new HudPageBuilder());
}

const wrote = (id: ContainerId) => paints.filter((p) => p.containerID === id);

/**
 * The lens path. Since 2026-09-11 the native status line carries only the
 * phone's own facts (clock · MIC · battery) and the Mac's state word
 * ("hearing you…", "reconnecting") is drawn by the renderer's spine from the
 * line Chrome composes into the StatusBar stream — one top-left tile per
 * state change. A frame-recording painter observes that composed line.
 */
const frames: HudVisualFrame[] = [];

function visualPresenter(): HudPresenter {
  const client = {
    require: () => ({
      textContainerUpgrade: async (u: TextContainerUpgrade) => {
        paints.push(u);
        return true;
      },
      rebuildPageContainer: async () => {
        rebuilds += 1;
        return true;
      },
    }),
  } as unknown as BridgeClient;
  return new HudPresenter(client, new HudPageBuilder(), {
    paint: async (frame) => {
      frames.push(frame);
      return true;
    },
  });
}

/** The frame-status line Chrome last composed, as the spine reads it. */
const lastStatus = () => frames[frames.length - 1]!.status;
const lastSpine = () => hudSpineLabel(frames[frames.length - 1]!);

beforeEach(() => {
  vi.useFakeTimers();
  paints.length = 0;
  frames.length = 0;
  rebuilds = 0;
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Chrome', () => {
  it('retires transient and clock writes, then resumes one clock on restart', async () => {
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.start();
    chrome.setMacState('working');
    chrome.stop();
    paints.length = 0;
    chrome.setLink('closed');
    chrome.setMic(false);
    chrome.setBattery(70);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(paints).toEqual([]);
    chrome.start();
    chrome.start();
    await vi.advanceTimersByTimeAsync(1000);
    paints.length = 0;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(wrote(ContainerId.StatusBar)).toHaveLength(1);
    chrome.stop();
  });

  it('turns the clock as the minute begins, once a minute (2026-10-06)', async () => {
    // A free-running 30 s interval turned the home clock up to 30 s late.
    vi.setSystemTime(new Date(2026, 9, 6, 9, 41, 30, 0));
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.start();
    await vi.advanceTimersByTimeAsync(0);
    paints.length = 0;
    await vi.advanceTimersByTimeAsync(29_000);       // 09:41:59
    expect(wrote(ContainerId.StatusBar)).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1_100);        // 09:42:00.1
    const turned = wrote(ContainerId.StatusBar);
    expect(turned).toHaveLength(1);
    expect(turned[0]?.content).toContain('9:42');
    paints.length = 0;
    await vi.advanceTimersByTimeAsync(59_000);       // 09:42:59.1
    expect(wrote(ContainerId.StatusBar)).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1_000);        // 09:43:00.1
    expect(wrote(ContainerId.StatusBar)).toHaveLength(1);
    chrome.stop();
  });

  it('paints clock, state and battery into the status bar on start', () => {
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.setBattery(87, false);
    chrome.start();
    chrome.stop();
    const bar = wrote(ContainerId.StatusBar);
    expect(bar.length).toBeGreaterThan(0);
    // clock · label · mic glyph · battery glyph (87% is seven blocks of
    // eight), the firmware line HudStatus composes from the model.
    expect(bar[bar.length - 1]!.content).toMatch(/^\d{1,2}:\d{2} · JOAT · CONNECTING · ○ · ▇$/);
  });

  it('keeps the status bar within the 46-character budget', () => {
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.setBattery(100, true);
    chrome.setLink('reconnecting');
    chrome.start();
    chrome.stop();
    for (const p of wrote(ContainerId.StatusBar)) {
      expect(p.content!.length).toBeLessThanOrEqual(46);
    }
  });

  it('reserves the battery slot before truncating a long Mac status', async () => {
    const hud = visualPresenter();
    const chrome = new Chrome(hud);
    chrome.setBattery(100, false);
    chrome.setLink('connected');
    chrome.setMacState('brainstorm connection test preparing reconnect');
    chrome.start();
    await vi.advanceTimersByTimeAsync(0);
    chrome.stop();

    // The composed line keeps the clock and the battery and shortens only
    // the state word, within the 46-character status wire budget.
    const content = lastStatus();
    expect(content).toContain('JOAT · BRAINSTORM CONNECTION TEST PREPA…');
    expect(content).toMatch(/ · ○ · █$/);
    // The frame carries the facts as data; the spine draws the shortened
    // word from them, and the battery is an icon beside it, not a word.
    expect(frames[frames.length - 1]!.statusModel).toMatchObject({
      link: 'connected', mic: false, battery: 100, charging: false,
      macState: 'brainstorm connection test preparing reconnect',
    });
    expect(lastSpine()).toBe('JOAT · BRAINSTORM CONNECTION TEST PREPA…');
    expect(lastSpine()).not.toContain('BAT');
  });

  it('carries the Mac\'s word on the native line only while it is current', async () => {
    // The firmware header used to keep to the phone's own facts because a
    // firmware page could not be refreshed in place, so a transient word
    // would have frozen there. It is refreshed in place now, and Chrome
    // still retires the word after twelve seconds, so the line shows it
    // while it is true and drops it when it is not.
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.setBattery(100, false);
    chrome.setLink('connected');
    chrome.setMacState('brainstorm connection test preparing reconnect');
    chrome.start();
    await vi.advanceTimersByTimeAsync(0);
    // Shown fitted to the firmware line: the word is cut, never the glyphs.
    expect(wrote(ContainerId.StatusBar).pop()!.content).toMatch(/^\d{1,2}:\d{2} · JOAT · BRAINSTORM CONNECTION TEST PREPA… · ○ · █$/);
    await vi.advanceTimersByTimeAsync(12_500);
    chrome.stop();
    const content = wrote(ContainerId.StatusBar).pop()!.content!;
    expect(content).toMatch(/^\d{1,2}:\d{2} · JOAT · ○ · █$/);
  });

  it('treats the plugin\'s own "listening" and "mic off" announcements as rest', async () => {
    // CapabilityMenu and DeviceMonitor still announce the mic in words.
    // Those restate the glyph; as a state word they hid the page's name.
    const hud = visualPresenter();
    const chrome = new Chrome(hud);
    chrome.setBattery(87, false);
    chrome.setLink('connected');
    chrome.start();
    await vi.advanceTimersByTimeAsync(0);
    await hud.setStatus('JOAT · mic off');
    await vi.advanceTimersByTimeAsync(0);
    expect(lastSpine()).toBe('JOAT');
    expect(frames[frames.length - 1]!.statusModel?.macState).toBeUndefined();
    await hud.setStatus('JOAT · listening');
    await vi.advanceTimersByTimeAsync(0);
    expect(lastSpine()).toBe('JOAT');
    // A real word still shows, and still retires.
    await hud.setStatus('JOAT · glasses on');
    await vi.advanceTimersByTimeAsync(0);
    expect(lastSpine()).toBe('JOAT · GLASSES ON');
    chrome.stop();
  });

  it('reports the LINK before the mic — a wearer whose brain is gone needs that first', async () => {
    const hud = visualPresenter();
    const chrome = new Chrome(hud);
    chrome.setMic(true);
    chrome.setLink('reconnecting');
    chrome.start();
    await vi.advanceTimersByTimeAsync(0);
    chrome.stop();
    // The state word is the link, not "listening": the spine says so, and
    // the microphone is a glyph on the line, never a word.
    expect(lastStatus()).toContain('JOAT · RECONNECTING');
    expect(lastStatus()).not.toContain('listening');
    expect(lastSpine()).toBe('JOAT · RECONNECTING');
    // The microphone keeps its own structured slot, on the lens and on the
    // native status line alike.
    expect(frames[frames.length - 1]!.micOn).toBe(true);
    expect(frames[frames.length - 1]!.statusModel?.mic).toBe(true);
    const native = realPresenter();
    const nativeChrome = new Chrome(native);
    nativeChrome.setMic(true);
    nativeChrome.setLink('reconnecting');
    nativeChrome.start();
    nativeChrome.stop();
    expect(wrote(ContainerId.StatusBar).pop()!.content).toMatch(/^\d{1,2}:\d{2} · JOAT · RECONNECTING · ●$/);
  });

  it('a 30s tick during a focus page NEVER rebuilds the page', async () => {
    // The reported bug, pinned at the Chrome end.
    const hud = realPresenter();
    await hud.showList(['Messages', 'Notifications']);
    const rebuildsAfterOpen = rebuilds;
    const chrome = new Chrome(hud);
    chrome.start();
    vi.advanceTimersByTime(90_000); // three ticks
    await Promise.resolve();
    chrome.stop();
    expect(rebuilds).toBe(rebuildsAfterOpen); // the list survives
    expect(wrote(ContainerId.Hint)).toEqual([]); // never written behind focus
  });

  it('setStatus from anywhere becomes the state word, not a second writer', async () => {
    const hud = visualPresenter();
    const chrome = new Chrome(hud);
    chrome.setBattery(64, false);
    chrome.setLink('connected');
    chrome.start();
    await vi.advanceTimersByTimeAsync(0);
    frames.length = 0;
    await hud.setStatus('JOAT · settings');
    await vi.advanceTimersByTimeAsync(0);
    chrome.stop();
    expect(frames.length).toBeGreaterThan(0);
    // Composed INTO the line — clock and battery survive the announcement,
    // and nothing paints the raw announcement as a line of its own.
    expect(frames.every((frame) => frame.status !== 'JOAT · settings')).toBe(true);
    expect(lastStatus()).toMatch(/^\d{1,2}:\d{2} · JOAT · SETTINGS · ○ · ▅$/);
    expect(lastSpine()).toBe('JOAT · SETTINGS');
    // The native status line has the same single writer: the announcement
    // never reaches the firmware container as a second, competing write.
    const native = realPresenter();
    const nativeChrome = new Chrome(native);
    nativeChrome.setLink('connected');
    nativeChrome.start();
    paints.length = 0;
    await native.setStatus('JOAT · settings');
    const bar = wrote(ContainerId.StatusBar);
    nativeChrome.stop();
    expect(bar.length).toBeGreaterThan(0);
    expect(bar.every((p) => p.content !== 'JOAT · settings')).toBe(true);
    expect(bar[bar.length - 1]!.content).toMatch(/^\d{1,2}:\d{2} · JOAT · SETTINGS · ○$/);
  });

  it('stop() halts the interval so a torn-down Chrome cannot paint', () => {
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.start();
    chrome.stop();
    paints.length = 0;
    vi.advanceTimersByTime(120_000);
    expect(paints).toEqual([]);
  });
});
