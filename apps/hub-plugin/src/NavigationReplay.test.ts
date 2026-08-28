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
      rebuildPageContainer: async () => true,
    }),
  } as unknown as BridgeClient;
  const renderer: HudFrameRenderer = {
    render: async (frame: HudVisualFrame) => {
      if (frame.kind === 'navigation') views.push(frame.navigation.view);
      return [
        { containerId: ContainerId.Image, pixels: new Uint8Array(4) },
        { containerId: ContainerId.VisualTopRight, pixels: new Uint8Array(4) },
        { containerId: ContainerId.VisualBottomLeft, pixels: new Uint8Array(4) },
        { containerId: ContainerId.VisualBottomRight, pixels: new Uint8Array(4) },
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

function expectEveryStateAfterFourTiles(events: readonly string[]): void {
  events.forEach((event, index) => {
    if (!event.startsWith('state:')) return;
    expect(events.slice(index - 4, index)).toEqual([
      `tile:${ContainerId.Image}`,
      `tile:${ContainerId.VisualTopRight}`,
      `tile:${ContainerId.VisualBottomLeft}`,
      `tile:${ContainerId.VisualBottomRight}`,
    ]);
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
    expect(replay.views).toEqual(['uncertain', 'guidance', 'uncertain', 'off_route']);
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
    expect(replay.views).toContain('wrong_way');
    expect(replay.views[replay.views.length - 1]).toBe('arrived');
    expect(replay.locationActive()).toBe(false);
    expectEveryStateAfterFourTiles(replay.events);
  });
});
