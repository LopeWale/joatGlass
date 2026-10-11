import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ImageRawDataUpdateResult } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { G2TraceEvent } from './G2DiagnosticTrace';
import type { HudStatusModel } from './HudStatus';
import {
  HudVisualSurface,
  PAGE_PART_FAILURE_LIMIT,
  PAGE_PART_UPLOAD_TIMEOUT_MS,
  PAGE_TRANSITION_IDLE_MS,
  TILE_UPLOAD_TIMEOUT_MS,
  type HudFrameRenderer,
  type HudVisualFrame,
} from './HudVisualSurface';
import { ContainerId, HeaderImageId } from './types';

/**
 * On the glasses (2026-10-08/09) one image write never settled. Every page
 * change then waited the full 15 s for it, MacLink's 15 s downlink watchdog
 * dropped the Mac link on the same beat, and the plugin reconnected into
 * the same wait: about 2,350 drops in two days. These pin the way out: a
 * stuck page part switches page parts off and frees the lane, a stuck full
 * tile is refused at once instead of waited on, and no page change waits
 * as long as the watchdog.
 */

const MODEL: HudStatusModel = { clock: '9:41', link: 'connected', mic: false, battery: 80, charging: false };
const FRAME: HudVisualFrame = {
  kind: 'overview', status: '', micOn: false, mainBrightness: 4, main: '', context: '', tray: '', hint: '',
};

function harness(answer: (containerId: number, call: number) => Promise<ImageRawDataUpdateResult>) {
  const calls: number[] = [];
  const events: G2TraceEvent[] = [];
  const client = {
    require: () => ({
      updateImageRawData: (update: { containerID: number }) => {
        calls.push(update.containerID);
        return answer(update.containerID, calls.length);
      },
    }),
  } as unknown as BridgeClient;
  let header = 0;
  const renderer: HudFrameRenderer = {
    render: async () => [4, 8, 9, 10].map((containerId) => ({ containerId, pixels: new Uint8Array([containerId]) })),
    // A new pixel every call, so the header is never skipped as unchanged.
    renderHeader: async () => {
      header += 1;
      return [
        { containerId: HeaderImageId.Left, pixels: new Uint8Array([header]), width: 1, height: 1 },
        { containerId: HeaderImageId.Right, pixels: new Uint8Array([header]), width: 1, height: 1 },
      ];
    },
  };
  const surface = new HudVisualSurface(client, renderer, { record: (event) => { events.push(event); } });
  const disabled = vi.fn();
  surface.onPagePartsDisabled = disabled;
  return { surface, calls, events, disabled };
}

const never = (): Promise<ImageRawDataUpdateResult> => new Promise(() => undefined);
const ok = (): Promise<ImageRawDataUpdateResult> => Promise.resolve(ImageRawDataUpdateResult.success);

beforeEach(() => { vi.useFakeTimers(); vi.spyOn(console, 'error').mockImplementation(() => undefined); vi.spyOn(console, 'warn').mockImplementation(() => undefined); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('a stuck image write never holds the Mac link', () => {
  it('a page-part write that never settles switches page parts off and frees the lane for the next page', async () => {
    const h = harness((id) => (id === HeaderImageId.Left ? never() : ok()));
    const painted = h.surface.paintHeader(MODEL, 'JOAT', () => true);
    await vi.advanceTimersByTimeAsync(PAGE_PART_UPLOAD_TIMEOUT_MS + 1);
    expect(await painted).toBe(false);
    // Reported with its real container, which the Mac now accepts.
    expect(h.events).toContainEqual(expect.objectContaining({
      type: 'tile_upload', containerId: HeaderImageId.Left, outcome: 'error',
    }));
    expect(h.surface.canPaintHeader()).toBe(false);
    expect(h.surface.canPaintHomeColumn()).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(h.disabled).toHaveBeenCalledTimes(1);
    // The hung header write no longer blocks the page that replaces it.
    const replace = vi.fn(async () => true);
    const changed = h.surface.withPageTransition(replace, () => true);
    await vi.advanceTimersByTimeAsync(10);
    expect(await changed).toBe(true);
    expect(replace).toHaveBeenCalledTimes(1);
    // And nothing paints a header again this session.
    expect(await h.surface.paintHeader(MODEL, 'JOAT', () => true)).toBe(false);
  });

  it('a page part gives up well before the link watchdog, not after the 15 s tile timeout', () => {
    expect(PAGE_PART_UPLOAD_TIMEOUT_MS).toBeLessThan(TILE_UPLOAD_TIMEOUT_MS);
    expect(PAGE_TRANSITION_IDLE_MS).toBeLessThan(15_000);
    expect(PAGE_PART_UPLOAD_TIMEOUT_MS).toBeLessThan(PAGE_TRANSITION_IDLE_MS);
  });

  it('a page change behind a stuck full tile is refused inside the watchdog, never by waiting 15 s', async () => {
    const h = harness((id) => (id === ContainerId.Image ? never() : ok()));
    void h.surface.paint(FRAME);
    await vi.advanceTimersByTimeAsync(1);
    const replace = vi.fn(async () => true);
    let settledAt: number | undefined;
    const started = Date.now();
    const changed = h.surface.withPageTransition(replace, () => true).then((value) => {
      settledAt = Date.now() - started;
      return value;
    });
    await vi.advanceTimersByTimeAsync(PAGE_TRANSITION_IDLE_MS + 1);
    expect(await changed).toBe(false);
    // Never replaced over an unanswered write (one writer), and answered
    // long before MacLink's 15 s watchdog would drop the link.
    expect(replace).not.toHaveBeenCalled();
    expect(settledAt).toBeLessThanOrEqual(PAGE_TRANSITION_IDLE_MS + 1);
  });

  it('once a full tile is known hung, later frames and page changes are refused at once', async () => {
    const h = harness((_id, call) => (call === 1 ? never() : ok()));
    const first = h.surface.paint(FRAME);
    await vi.advanceTimersByTimeAsync(TILE_UPLOAD_TIMEOUT_MS + 1);
    expect(await first).toBe(false);
    const second = h.surface.paint(FRAME);
    await vi.advanceTimersByTimeAsync(1);
    expect(await second).toBe(false);
    const replace = vi.fn(async () => true);
    const changed = h.surface.withPageTransition(replace, () => true);
    await vi.advanceTimersByTimeAsync(1);
    expect(await changed).toBe(false);
    expect(h.calls).toHaveLength(1);
  });

  it(`${PAGE_PART_FAILURE_LIMIT} failed page-part sends in a row switch page parts off`, async () => {
    const h = harness((id) => Promise.resolve(id === HeaderImageId.Left
      ? ImageRawDataUpdateResult.sendFailed : ImageRawDataUpdateResult.success));
    for (let attempt = 0; attempt < PAGE_PART_FAILURE_LIMIT; attempt += 1) {
      expect(h.surface.canPaintHeader()).toBe(true);
      const painted = h.surface.paintHeader(MODEL, 'JOAT', () => true);
      await vi.advanceTimersByTimeAsync(1);
      expect(await painted).toBe(false);
    }
    expect(h.surface.canPaintHeader()).toBe(false);
    expect(h.events.filter((event) => event.type === 'tile_upload' && event.outcome === 'failed'))
      .toHaveLength(PAGE_PART_FAILURE_LIMIT);
  });
});
