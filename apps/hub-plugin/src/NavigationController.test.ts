import { describe, expect, it, vi } from 'vitest';

import { NavigationController } from './NavigationController';
import { navigationVisualFixture, type NavigationHudModel } from './NavigationHud';
import type { UplinkMessage } from './types';

function harness(paintResults: boolean[] = []) {
  const frames: unknown[] = [];
  const sent: UplinkMessage[] = [];
  const allSent: UplinkMessage[] = [];
  const diagnostics = { record: vi.fn() };
  let locationConsumer: ((location: never, receivedAt: number) => void) | undefined;
  const location = {
    setNavigationConsumer: async (consumer?: typeof locationConsumer) => {
      locationConsumer = consumer;
      return true;
    },
    requestNavigationOrigin: async () => ({
      latitude: 41.9, longitude: -87.65, accuracy: 6,
    }),
  };
  const controller = new NavigationController(
    {
      showNavigation: async (frame) => {
        frames.push(frame);
        return paintResults.shift() ?? true;
      },
    },
    { sendNavigation: (message) => {
      allSent.push(message);
      if (message.kind !== 'navigation.view.ack') sent.push(message);
    } },
    location,
    diagnostics,
  );
  return {
    controller,
    frames,
    sent,
    allSent,
    diagnostics,
    emit: (locationFix: object, receivedAt = 10_100) => {
      locationConsumer?.(locationFix as never, receivedAt);
    },
  };
}

const routePlan = {
  mode: 'walking' as const,
  geometry: [
    { latitude: 0, longitude: 0 },
    { latitude: 0, longitude: 0.01 },
  ],
  steps: [
    { id: 'turn', maneuver: 'turn_right' as const, instruction: 'onto Test Ave', distanceAlongRouteM: 500 },
    { id: 'arrive', maneuver: 'arrive' as const, instruction: 'Cajun Corner', distanceAlongRouteM: 1_111 },
  ],
  policy: {
    maxAccuracyM: 20,
    staleAfterMs: 5_000,
    corridorRadiusM: 30,
    ambiguityMarginM: 5,
    ambiguityProgressDeltaM: 100,
    searchBackM: 120,
    searchForwardM: 500,
    minCourseDisplacementM: 5,
    wrongWayAngleDeg: 120,
    wrongWayMinBacktrackM: 15,
    wrongWaySamples: 2,
    offRouteSamples: 2,
    arrivalRadiusM: 15,
    arrivalSamples: 2,
    uTurnSuppressionM: 50,
  },
};

describe('NavigationController', () => {
  it('maps a G2 single tap to the destination confirmation action', async () => {
    const { controller, sent } = harness();
    await controller.present({
      kind: 'navigation.view',
      sessionId: 'nav-1',
      requestId: 'place-2',
      generation: 4,
      navigation: navigationVisualFixture('nav-destination'),
    });

    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([{
      kind: 'navigation.action',
      sessionId: 'nav-1',
      requestId: 'place-2',
      generation: 4,
      action: 'confirm_destination',
    }]);
  });

  it('returns a fresh explicit route origin bound to the request generation', async () => {
    const { controller, sent } = harness();
    expect(await controller.requestOrigin({
      kind: 'navigation.origin.request',
      sessionId: 'nav-1', requestId: 'origin-1', generation: 6,
    })).toBe(true);
    expect(sent).toEqual([{
      kind: 'navigation.origin',
      sessionId: 'nav-1', requestId: 'origin-1', generation: 6,
      latitude: 41.9, longitude: -87.65, accuracy: 6,
    }]);
  });

  it('returns an exact failure receipt when a fresh route origin is unavailable', async () => {
    const allSent: UplinkMessage[] = [];
    const controller = new NavigationController(
      { showNavigation: async () => true },
      { sendNavigation: (message) => { allSent.push(message); } },
      {
        setNavigationConsumer: async () => true,
        requestNavigationOrigin: async () => null,
      },
    );

    expect(await controller.requestOrigin({
      kind: 'navigation.origin.request',
      sessionId: 'nav-1', requestId: 'origin-1', generation: 6,
    })).toBe(false);
    expect(allSent).toEqual([{
      kind: 'navigation.origin.failed',
      sessionId: 'nav-1', requestId: 'origin-1', generation: 6,
      reason: 'unavailable',
    }]);
  });

  it('acknowledges only the exact navigation view that the HUD accepted', async () => {
    const { controller, allSent } = harness([false, true]);
    const first = {
      kind: 'navigation.view' as const,
      sessionId: 'nav-1', requestId: 'mode-1', generation: 1,
      navigation: navigationVisualFixture('nav-mode'),
    };
    expect(await controller.present(first)).toBe(false);
    expect(controller.isActive()).toBe(false);
    expect(await controller.present({ ...first, requestId: 'mode-2', generation: 2 })).toBe(true);
    expect(controller.isActive()).toBe(true);
    expect(allSent).toEqual([
      {
        kind: 'navigation.view.ack', sessionId: 'nav-1', requestId: 'mode-1',
        generation: 1, outcome: 'rejected',
      },
      {
        kind: 'navigation.view.ack', sessionId: 'nav-1', requestId: 'mode-2',
        generation: 2, outcome: 'bridge_acknowledged',
      },
    ]);
  });

  it('records the acknowledged HUD semantic state without its visible copy', async () => {
    const { controller, diagnostics } = harness();

    await controller.present({
      kind: 'navigation.view', sessionId: 'nav-1', requestId: 'mode-1', generation: 1,
      navigation: navigationVisualFixture('nav-mode'),
    });

    expect(diagnostics.record).toHaveBeenCalledWith({
      type: 'navigation_cue', cue: 'mode', outcome: 'success',
    });
    expect(JSON.stringify(diagnostics.record.mock.calls)).not.toContain('Walking');
  });

  it('cannot reactivate a stopped session when an old HUD paint finishes late', async () => {
    let releasePaint!: (painted: boolean) => void;
    const pendingPaint = new Promise<boolean>((resolve) => { releasePaint = resolve; });
    const allSent: UplinkMessage[] = [];
    const controller = new NavigationController(
      { showNavigation: () => pendingPaint },
      { sendNavigation: (message) => { allSent.push(message); } },
    );
    const presenting = controller.present({
      kind: 'navigation.view', sessionId: 'nav-old', requestId: 'mode-old', generation: 1,
      navigation: navigationVisualFixture('nav-mode'),
    });
    await Promise.resolve();

    // Even though input ownership is not committed until all four tiles finish,
    // the caller must know a navigation paint is in flight so it can queue an
    // Overview restoration behind those obsolete pixels.
    expect(await controller.stopAll()).toBe(true);
    releasePaint(true);

    expect(await presenting).toBe(false);
    expect(controller.isActive()).toBe(false);
    expect(allSent).toEqual([]);
  });

  it('drops a late origin result after navigation ownership is revoked', async () => {
    let releaseOrigin!: (origin: { latitude: number; longitude: number }) => void;
    const pendingOrigin = new Promise<{ latitude: number; longitude: number }>(
      (resolve) => { releaseOrigin = resolve; },
    );
    const allSent: UplinkMessage[] = [];
    const controller = new NavigationController(
      { showNavigation: async () => true },
      { sendNavigation: (message) => { allSent.push(message); } },
      {
        setNavigationConsumer: async () => true,
        requestNavigationOrigin: () => pendingOrigin,
      },
    );
    const requesting = controller.requestOrigin({
      kind: 'navigation.origin.request', sessionId: 'nav-old',
      requestId: 'origin-old', generation: 2,
    });
    await Promise.resolve();

    await controller.stopAll();
    releaseOrigin({ latitude: 41.9, longitude: -87.65 });

    expect(await requesting).toBe(false);
    expect(allSent).toEqual([]);
  });

  it('uses swipes only to move the highlighted repair choice, then taps its stable ID', async () => {
    const { controller, frames, sent } = harness();
    await controller.present({
      kind: 'navigation.view',
      sessionId: 'nav-1',
      requestId: 'repair-3',
      generation: 5,
      navigation: navigationVisualFixture('nav-repair'),
    });

    expect(await controller.handleScroll('down')).toBe(true);
    expect(frames[frames.length - 1]).toMatchObject({ selectedIndex: 1 });
    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([{
      kind: 'navigation.action',
      sessionId: 'nav-1',
      requestId: 'repair-3',
      generation: 5,
      action: 'select_destination',
      choiceId: 'place-chicago',
    }]);
  });

  it('rejects an older generation without repainting or rebinding input', async () => {
    const { controller, frames, sent } = harness();
    expect(await controller.present({
      kind: 'navigation.view',
      sessionId: 'nav-1',
      requestId: 'mode-2',
      generation: 8,
      navigation: navigationVisualFixture('nav-mode'),
    })).toBe(true);
    expect(await controller.present({
      kind: 'navigation.view',
      sessionId: 'nav-1',
      requestId: 'place-old',
      generation: 7,
      navigation: navigationVisualFixture('nav-destination'),
    })).toBe(false);

    expect(frames).toHaveLength(1);
    expect(controller.handleClick()).toBe(true);
    expect(sent[0]).toMatchObject({
      generation: 8,
      requestId: 'mode-2',
      action: 'select_mode',
      choiceId: 'walking',
    });
  });

  it('consumes movement input without emitting a route action', async () => {
    const { controller, sent } = harness();
    await controller.present({
      kind: 'navigation.view',
      sessionId: 'nav-1',
      requestId: 'route-1',
      generation: 9,
      navigation: navigationVisualFixture('nav-guidance'),
    });

    expect(controller.handleClick()).toBe(true);
    expect(await controller.handleScroll('up')).toBe(true);
    expect(sent).toEqual([]);
  });

  it('sends a single bound cancel action for the explicit left-temple navigation exit', async () => {
    const { controller, sent } = harness();
    await controller.present({
      kind: 'navigation.view',
      sessionId: 'nav-1', requestId: 'guidance-1', generation: 9,
      navigation: navigationVisualFixture('nav-guidance'),
    });

    expect(controller.handleCancel()).toBe(true);
    expect(sent).toEqual([{
      kind: 'navigation.action',
      sessionId: 'nav-1', requestId: 'guidance-1', generation: 9,
      action: 'cancel',
    }]);
  });

  it('clears a session that stops before any route was installed', async () => {
    const { controller, diagnostics } = harness();
    await controller.present({
      kind: 'navigation.view',
      sessionId: 'nav-1',
      requestId: 'provider-error',
      generation: 1,
      navigation: navigationVisualFixture('nav-error'),
    });

    expect(controller.isActive()).toBe(true);
    expect(await controller.stopRoute('different-session')).toBe(false);
    expect(controller.isActive()).toBe(true);
    expect(await controller.stopRoute('nav-1')).toBe(true);
    expect(controller.isActive()).toBe(false);
    expect(diagnostics.record).toHaveBeenCalledWith({
      type: 'navigation_cue', cue: 'stopped', outcome: 'success',
    });
  });

  it('drops local HUD and high-rate location ownership when the Mac link is lost', async () => {
    const { controller } = harness();
    await controller.present({
      kind: 'navigation.view', sessionId: 'nav-1', requestId: 'mode-1', generation: 1,
      navigation: navigationVisualFixture('nav-mode'),
    });
    await controller.installRoute({
      kind: 'navigation.route', sessionId: 'nav-1', requestId: 'route-1',
      generation: 2, routeId: 'route-1', revision: 1,
      destinationLabel: 'Cajun Corner', etaMinutes: 18, plan: routePlan,
    });

    expect(await controller.stopAll()).toBe(true);
    expect(controller.isActive()).toBe(false);
  });


  it('adopts street surroundings that arrive before their route', async () => {
    // The Mac fetches the map as soon as it has the wearer's ORIGIN, which
    // happens before it has a route to send. Dropping the tile because no
    // route existed yet is exactly why guidance kept falling back to a
    // bare polyline on black.
    const { controller, frames, emit } = harness();
    const image = {} as CanvasImageSource;
    await controller.installMapPanel(
      {
        kind: 'navigation.map', sessionId: 'nav-1',
        latitude: 41.9, longitude: -87.65,
        zoom: 16, width: 248, height: 168, png: 'ignored',
      },
      async () => image,
    );
    await controller.installRoute({
      kind: 'navigation.route', sessionId: 'nav-1', requestId: 'route-1',
      generation: 2, routeId: 'route-1', revision: 1,
      destinationLabel: 'Cajun Corner', etaMinutes: 18, plan: routePlan,
    });
    emit({ latitude: 0, longitude: 0.0009, accuracy: 5, timestamp: 10_000 });
    await controller.whenIdle();

    const guidance = frames
      .map((frame) => frame as NavigationHudModel)
      .find((frame) => frame?.map?.panel !== undefined);
    expect(guidance?.map?.panel?.image).toBe(image);
  });

  it('keeps a map that belongs to a different session out of this route', async () => {
    const { controller, frames, emit } = harness();
    await controller.installMapPanel(
      {
        kind: 'navigation.map', sessionId: 'nav-OTHER',
        latitude: 41.9, longitude: -87.65,
        zoom: 16, width: 248, height: 168, png: 'ignored',
      },
      async () => ({} as CanvasImageSource),
    );
    await controller.installRoute({
      kind: 'navigation.route', sessionId: 'nav-1', requestId: 'route-1',
      generation: 2, routeId: 'route-1', revision: 1,
      destinationLabel: 'Cajun Corner', etaMinutes: 18, plan: routePlan,
    });
    emit({ latitude: 0, longitude: 0.0009, accuracy: 5, timestamp: 10_000 });
    await controller.whenIdle();
    const leaked = frames
      .map((frame) => frame as NavigationHudModel)
      .some((frame) => frame?.map?.panel !== undefined);
    expect(leaked).toBe(false);
  });

  it('survives a host with no image decoder instead of throwing', async () => {
    const { controller } = harness();
    await expect(controller.installMapPanel(
      {
        kind: 'navigation.map', sessionId: 'nav-1',
        latitude: 41.9, longitude: -87.65,
        zoom: 16, width: 248, height: 168, png: 'ignored',
      },
      async () => { throw new Error('no decoder'); },
    )).resolves.toBeUndefined();
  });

  it('rejects route installation when high-rate location activation throws', async () => {
    const sent: UplinkMessage[] = [];
    const controller = new NavigationController(
      { showNavigation: async () => true },
      { sendNavigation: (message) => { sent.push(message); } },
      {
        setNavigationConsumer: async () => { throw new Error('host denied location'); },
        requestNavigationOrigin: async () => null,
      },
    );
    expect(await controller.installRoute({
      kind: 'navigation.route', sessionId: 'nav-1', requestId: 'route-1',
      generation: 2, routeId: 'route-1', revision: 1,
      destinationLabel: 'Cajun Corner', etaMinutes: 18, plan: routePlan,
    })).toBe(false);
    expect(sent).toEqual([{
      kind: 'navigation.route.ack', sessionId: 'nav-1', routeId: 'route-1',
      revision: 1, outcome: 'rejected',
    }]);
  });

  it('installs a route, keeps rapid fixes local, and emits only semantic progress', async () => {
    const { controller, emit, frames, sent } = harness();
    expect(await controller.installRoute({
      kind: 'navigation.route',
      sessionId: 'nav-1',
      requestId: 'route-request-1',
      generation: 10,
      routeId: 'route-1',
      revision: 1,
      destinationLabel: 'Cajun Corner',
      etaMinutes: 18,
      plan: routePlan,
    })).toBe(true);

    emit({ latitude: 0, longitude: 0.0009, accuracy: 5, timestamp: 10_000 });
    await controller.whenIdle();

    expect(sent[0]).toEqual({
      kind: 'navigation.route.ack',
      sessionId: 'nav-1', routeId: 'route-1', revision: 1, outcome: 'installed',
    });
    expect(sent[1]).toMatchObject({
      kind: 'navigation.state', state: 'guidance', routeId: 'route-1', revision: 1,
    });
    expect(JSON.stringify(sent.slice(0, 2))).not.toContain('latitude');
    expect(frames[frames.length - 1]).toMatchObject({
      view: 'guidance', maneuver: 'turn_right', secondary: 'onto Test Ave',
    });
  });

  it('sends one explicit reroute coordinate only after sustained off-route evidence', async () => {
    const { controller, emit, sent } = harness();
    await controller.installRoute({
      kind: 'navigation.route', sessionId: 'nav-1', requestId: 'route-request-1',
      generation: 10, routeId: 'route-1', revision: 1,
      destinationLabel: 'Cajun Corner', etaMinutes: 18, plan: routePlan,
    });
    emit({ latitude: 0.001, longitude: 0.001, accuracy: 5, timestamp: 10_000 });
    emit({ latitude: 0.001, longitude: 0.0011, accuracy: 5, timestamp: 11_000 }, 11_100);
    emit({ latitude: 0.001, longitude: 0.0012, accuracy: 5, timestamp: 12_000 }, 12_100);
    await controller.whenIdle();

    const reroutes = sent.filter((message) => message.kind === 'navigation.reroute.request');
    expect(reroutes).toHaveLength(1);
    expect(reroutes[0]).toMatchObject({
      baseRevision: 1, latitude: 0.001, longitude: 0.0011,
    });
    const countAfterReroute = sent.length;
    emit({ latitude: 0.001, longitude: 0.0013, accuracy: 5, timestamp: 13_000 }, 13_100);
    await controller.whenIdle();
    expect(sent).toHaveLength(countAfterReroute);
  });

  it('does not report a semantic state until the HUD bridge accepts that view', async () => {
    const { controller, emit, sent } = harness([false, true]);
    await controller.installRoute({
      kind: 'navigation.route', sessionId: 'nav-1', requestId: 'route-request-1',
      generation: 10, routeId: 'route-1', revision: 1,
      destinationLabel: 'Cajun Corner', etaMinutes: 18, plan: routePlan,
    });
    emit({ latitude: 0, longitude: 0.0009, accuracy: 5, timestamp: 10_000 });
    await controller.whenIdle();
    expect(sent.map((message) => message.kind)).toEqual(['navigation.route.ack']);

    emit({ latitude: 0, longitude: 0.001, accuracy: 5, timestamp: 11_000 }, 11_100);
    await controller.whenIdle();
    expect(sent.map((message) => message.kind)).toEqual([
      'navigation.route.ack', 'navigation.state',
    ]);
  });

  it('coalesces superseded guidance while the current G2 frame is still painting', async () => {
    let emit: ((locationFix: object, receivedAt?: number) => void) | undefined;
    let firstPaintStarted!: () => void;
    let finishFirstPaint!: (painted: boolean) => void;
    const firstPaint = new Promise<boolean>((resolve) => { finishFirstPaint = resolve; });
    const firstPaintHasStarted = new Promise<void>((resolve) => { firstPaintStarted = resolve; });
    const frames: NavigationHudModel[] = [];
    const sent: UplinkMessage[] = [];
    let paintCount = 0;
    const controller = new NavigationController(
      {
        showNavigation: (frame) => {
          frames.push(frame);
          paintCount += 1;
          if (paintCount === 1) {
            firstPaintStarted();
            return firstPaint;
          }
          return Promise.resolve(true);
        },
      },
      { sendNavigation: (message) => { sent.push(message); } },
      {
        setNavigationConsumer: async (consumer) => {
          emit = (locationFix, receivedAt = 10_100) => consumer?.(locationFix as never, receivedAt);
          return true;
        },
        requestNavigationOrigin: async () => null,
      },
    );
    await controller.installRoute({
      kind: 'navigation.route', sessionId: 'nav-1', requestId: 'route-request-1',
      generation: 10, routeId: 'route-1', revision: 1,
      destinationLabel: 'Cajun Corner', etaMinutes: 18, plan: routePlan,
    });

    emit?.({ latitude: 0, longitude: 0.0001, accuracy: 5, timestamp: 10_000 });
    await firstPaintHasStarted;
    emit?.({ latitude: 0, longitude: 0.001, accuracy: 5, timestamp: 11_000 }, 11_100);
    emit?.({ latitude: 0, longitude: 0.002, accuracy: 5, timestamp: 12_000 }, 12_100);
    finishFirstPaint(true);
    await controller.whenIdle();

    expect(frames).toHaveLength(2);
    expect(frames[0]).toMatchObject({ view: 'guidance' });
    expect(frames[1]).toMatchObject({ view: 'guidance', maneuverDistanceM: 278 });
    expect(sent.filter((message) => message.kind === 'navigation.state')).toHaveLength(2);
  });
});
