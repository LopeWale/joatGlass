import { describe, expect, it } from 'vitest';
import { ImageRawDataUpdateResult } from '@evenrealities/even_hub_sdk';

import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import {
  HudVisualSurface,
  type HudFrameRenderer,
  type HudVisualFrame,
} from './HudVisualSurface';
import { NavigationController } from './NavigationController';
import type { NavigationMode, NavigationView } from './NavigationHud';
import type { NavigationProgressPolicy, NavigationRoutePlan } from './NavigationProgress';
import { ContainerId, type UplinkMessage } from './types';

const METERS_PER_DEGREE = 111_195;

function point(xM: number, yM = 0) {
  return { latitude: yM / METERS_PER_DEGREE, longitude: xM / METERS_PER_DEGREE };
}

const replayPolicy: NavigationProgressPolicy = {
  maxAccuracyM: 20,
  staleAfterMs: 5_000,
  corridorRadiusM: 30,
  ambiguityMarginM: 5,
  ambiguityProgressDeltaM: 100,
  searchBackM: 150,
  searchForwardM: 500,
  minCourseDisplacementM: 5,
  wrongWayAngleDeg: 120,
  wrongWayMinBacktrackM: 15,
  wrongWaySamples: 2,
  offRouteSamples: 2,
  arrivalRadiusM: 15,
  arrivalSamples: 2,
  uTurnSuppressionM: 50,
};

function replayPlan(mode: NavigationMode): NavigationRoutePlan {
  return {
    mode,
    geometry: [point(0), point(1_000)],
    steps: [
      {
        id: 'turn', maneuver: 'turn_right', instruction: 'onto Test Ave',
        distanceAlongRouteM: 500,
      },
      {
        id: 'arrive', maneuver: 'arrive', instruction: 'Test destination',
        distanceAlongRouteM: 1_000,
      },
    ],
    policy: replayPolicy,
  };
}

function replayHarness(mode: NavigationMode) {
  const events: string[] = [];
  const views: NavigationView[] = [];
  const sent: UplinkMessage[] = [];
  let consumer: ((fix: never, receivedAt: number) => void) | undefined;

  const client = {
    require: () => ({
      updateImageRawData: async (update: { containerID?: number }) => {
        events.push(`tile:${update.containerID ?? -1}`);
        return ImageRawDataUpdateResult.success;
      },
      // A page with image containers is the four-tile map page; a page of
      // text containers is a frame before the live map (confirm the place,
      // a warning, arrival), Even's own text page.
      rebuildPageContainer: async (page: { imageObject?: unknown[] }) => {
        events.push(page.imageObject?.length ? 'page:image' : 'page:text');
        return true;
      },
      // A cursor move or a new step on the same text page upgrades it in place.
      textContainerUpgrade: async (update: { containerName?: string }) => {
        events.push(`upgrade:${update.containerName ?? '?'}`);
        return true;
      },
    }),
  } as unknown as BridgeClient;
  const renderer: HudFrameRenderer = {
    render: async (frame: HudVisualFrame) => {
      if (frame.kind === 'navigation') views.push(frame.navigation.view);
      // Every view draws different pixels in every region, as the real
      // renderer does for a changing state word, header and footer. The
      // visual surface skips a region whose delivered pixels are unchanged
      // on the same page, so identical tiles would hide the ordering this
      // replay exists to prove: a state is reported only after its frame.
      const revision = views.length;
      return [
        { containerId: ContainerId.Image, pixels: Uint8Array.of(revision, 1, 0, 0) },
        { containerId: ContainerId.VisualTopRight, pixels: Uint8Array.of(revision, 2, 0, 0) },
        { containerId: ContainerId.VisualBottomLeft, pixels: Uint8Array.of(revision, 3, 0, 0) },
        { containerId: ContainerId.VisualBottomRight, pixels: Uint8Array.of(revision, 4, 0, 0) },
      ];
    },
  };
  const hud = new HudPresenter(
    client,
    new HudPageBuilder(),
    new HudVisualSurface(client, renderer),
  );
  const controller = new NavigationController(
    hud,
    {
      sendNavigation: (message) => {
        sent.push(message);
        if (message.kind === 'navigation.state') events.push(`state:${message.state}`);
      },
    },
    {
      setNavigationConsumer: async (next) => {
        consumer = next as typeof consumer;
        return true;
      },
      requestNavigationOrigin: async () => ({ ...point(0), accuracy: 5 }),
    },
  );

  const install = () => controller.installRoute({
    kind: 'navigation.route',
    sessionId: `replay-${mode}`,
    requestId: `route-${mode}`,
    generation: 1,
    routeId: `route-${mode}`,
    revision: 1,
    destinationLabel: 'Test destination',
    etaMinutes: mode === 'walking' ? 12 : 5,
    plan: replayPlan(mode),
  });
  const emit = (xM: number, yM: number, timestamp: number, accuracy = 5) => {
    consumer?.({ ...point(xM, yM), accuracy, timestamp } as never, timestamp + 100);
  };
  return {
    controller,
    events,
    views,
    sent,
    install,
    emit,
    locationActive: () => consumer !== undefined,
  };
}

/** The live map is the one navigation frame drawn as pixels: a guidance
 * state follows its four tiles. Every other frame (uncertain, off-route,
 * arrival) is Even's own text page, so its state follows no tile at all. */
function expectEveryStateAfterFourTiles(events: readonly string[]): void {
  events.forEach((event, index) => {
    if (!event.startsWith('state:')) return;
    if (event === 'state:guidance') {
      expect(events.slice(index - 4, index)).toEqual([
        `tile:${ContainerId.Image}`,
        `tile:${ContainerId.VisualTopRight}`,
        `tile:${ContainerId.VisualBottomLeft}`,
        `tile:${ContainerId.VisualBottomRight}`,
      ]);
      return;
    }
    const previousState = events.slice(0, index).findLastIndex((entry) => entry.startsWith('state:'));
    const sinceLastState = events.slice(previousState + 1, index);
    expect(sinceLastState.filter((entry) => entry.startsWith('tile:'))).toEqual([]);
    expect(sinceLastState.some((entry) => entry === 'page:text' || entry === 'upgrade:main')).toBe(true);
  });
}

describe('navigation production-path replay', () => {
  it('holds noisy walking fixes, debounces off-route evidence, then requests one reroute', async () => {
    const replay = replayHarness('walking');
    expect(await replay.install()).toBe(true);

    replay.emit(100, 0, 10_000, 45);
    replay.emit(110, 0, 11_000);
    replay.emit(120, 100, 12_000);
    replay.emit(130, 100, 13_000);
    await replay.controller.whenIdle();

    expect(replay.sent.filter((message) => message.kind === 'navigation.state')
      .map((message) => message.state)).toEqual([
      'uncertain', 'guidance', 'uncertain', 'off_route',
    ]);
    expect(replay.sent.filter((message) => message.kind === 'navigation.reroute.request'))
      .toHaveLength(1);
    // The renderer draws only the live map; the warnings are text pages.
    expect(replay.views).toEqual(['guidance']);
    expectEveryStateAfterFourTiles(replay.events);
  });

  it('replays cycling reversal, recovery, and stable arrival before releasing location', async () => {
    const replay = replayHarness('cycling');
    expect(await replay.install()).toBe(true);

    replay.emit(100, 0, 18_000);
    replay.emit(500, 0, 19_000);
    replay.emit(700, 0, 20_000);
    replay.emit(650, 0, 21_000);
    replay.emit(600, 0, 22_000);
    replay.emit(650, 0, 23_000);
    replay.emit(900, 0, 24_000);
    replay.emit(990, 0, 25_000);
    replay.emit(997, 0, 26_000);
    await replay.controller.whenIdle();

    const states = replay.sent.filter((message) => message.kind === 'navigation.state')
      .map((message) => message.state);
    expect(states).toContain('wrong_way');
    expect(states.slice(states.indexOf('wrong_way') + 1)).toContain('guidance');
    expect(states[states.length - 1]).toBe('arrived');
    expect(replay.views).not.toContain('wrong_way');
    expect(replay.views.every((view) => view === 'guidance')).toBe(true);
    expect(replay.events[replay.events.length - 1]).toBe('state:arrived');
    expect(replay.locationActive()).toBe(false);
    expectEveryStateAfterFourTiles(replay.events);
  });
});
