import { isRouteProgressView } from './ActivityChips';
import type { NavigationHudModel } from './NavigationHud';
import { formatNavigationDistance, navigationStreetLabel } from './NavigationHud';
import {
  NavigationProgressTracker,
  type NavigationFix,
  type NavigationProgressState,
} from './NavigationProgress';
import { locationTimestampMs, type LocationSessionManager } from './LocationSessionManager';
import type { G2DiagnosticTrace, NavigationCue } from './G2DiagnosticTrace';
import type {
  DownlinkMessage,
  NavigationAction,
  UplinkMessage,
} from './types';

type NavigationViewMessage = Extract<DownlinkMessage, { kind: 'navigation.view' }>;
type NavigationRouteMessage = Extract<DownlinkMessage, { kind: 'navigation.route' }>;
type NavigationMapMessage = Extract<DownlinkMessage, { kind: 'navigation.map' }>;
type NavigationFixMessage = Extract<DownlinkMessage, { kind: 'navigation.fix' }>;

/**
 * The guidance page follows the wearer, not only the instruction. Until
 * 2026-10-05 it repainted only when the maneuver or the ROUNDED distance
 * changed — over a kilometre the distance rounds to 0.1 km, so the position
 * dot, the progress bar and the ETA froze for a hundred metres at a time.
 * Now a fix that moved the wearer repaints the page, at most once a second:
 * the queue below keeps only the newest guidance fix, and the four-tile
 * frame itself paces the BLE link, so this is a ceiling, not a rate.
 */
const REPAINT_MIN_INTERVAL_MS = 1_000;
/** About a pixel and a half at the schematic's walking scale: smaller
 * moves are not visible. */
const REPAINT_MIN_MOVE_M = 3;
const EARTH_RADIUS_M = 6_371_000;

function distanceM(
  a: { latitude: number; longitude: number },
  b: { latitude: number; longitude: number },
): number {
  const lat = ((a.latitude + b.latitude) / 2) * Math.PI / 180;
  const dx = (b.longitude - a.longitude) * Math.PI / 180 * EARTH_RADIUS_M * Math.cos(lat);
  const dy = (b.latitude - a.latitude) * Math.PI / 180 * EARTH_RADIUS_M;
  return Math.hypot(dx, dy);
}

type NavigationUplink = Extract<UplinkMessage, {
  kind: 'navigation.action' | 'navigation.route.ack' | 'navigation.state'
    | 'navigation.reroute.request' | 'navigation.origin'
    | 'navigation.view.ack' | 'navigation.origin.failed';
}>;

interface NavigationHudSink {
  showNavigation(navigation: NavigationHudModel, statusOverride?: string): Promise<boolean>;
  /** A route's progress: repaints when the route owns the glass, and
   * otherwise only updates the retained page and the header chip. */
  updateNavigation?(navigation: NavigationHudModel, statusOverride?: string): Promise<boolean>;
  /** Whether navigation currently owns the visible glass surface. */
  isNavigationVisible?(): boolean;
  /** The route ended: its chip leaves every header. */
  navigationEnded?(): void;
}

interface NavigationLink {
  sendNavigation(message: NavigationUplink): boolean | void;
}

interface NavigationLocationSession {
  setNavigationConsumer(
    consumer?: Parameters<LocationSessionManager['setNavigationConsumer']>[0],
  ): Promise<boolean>;
  requestNavigationOrigin(): ReturnType<LocationSessionManager['requestNavigationOrigin']>;
}

interface ActiveRoute {
  message: NavigationRouteMessage;
  tracker: NavigationProgressTracker;
  phoneFixes: boolean;
  sequence: number;
  lastSignature?: string;
  rerouteRequested: boolean;
  pausedForReroute: boolean;
  /**
   * Awaiting visual cues. Consecutive ordinary guidance fixes are coalesced,
   * but a warning or state transition always remains its own visible cue.
   */
  pendingProgress: Array<{ state: NavigationProgressState; fix: NavigationFix }>;
  progressRendering: boolean;
  /** When and where the guidance page was last painted, for the repaint pace. */
  lastPaintAt?: number;
  lastPaintedFix?: NavigationFix;
  /** The last progress shown, replayed by resumeToGlass. */
  lastProgress?: { state: NavigationProgressState; fix: NavigationFix };
  repaintRequested: boolean;
  reportedFixShape: boolean;
}

/**
 * Owns navigation input while a generation-bound navigation view is visible.
 * User-facing labels never select behavior; phase plus stable choice IDs do.
 */
export class NavigationController {
  private current?: NavigationViewMessage;
  private route?: ActiveRoute;
  private progressQueue: Promise<void> = Promise.resolve();
  private lifecycleEpoch = 0;
  private presentationOperation = 0;
  private pendingPresentation?: { operation: number; sessionId: string };
  private originOperation = 0;

  constructor(
    private readonly hud: NavigationHudSink,
    private readonly link: NavigationLink,
    private readonly location?: NavigationLocationSession,
    private readonly diagnostics?: G2DiagnosticTrace,
    private readonly clock: () => number = () => Date.now(),
  ) {}

  isActive(): boolean {
    return this.current !== undefined;
  }

  /** Deterministic replay/testing seam; live location delivery never waits. */
  whenIdle(): Promise<void> {
    return this.progressQueue;
  }

  async present(
    message: NavigationViewMessage,
    statusOverride?: string,
    signal?: AbortSignal,
  ): Promise<boolean> {
    const lifecycleEpoch = this.lifecycleEpoch;
    const operation = ++this.presentationOperation;
    if (
      this.current?.sessionId === message.sessionId
      && message.generation <= this.current.generation
    ) {
      this.sendViewAck(message, 'rejected');
      return false;
    }
    const candidate = this.cloneMessage(message);
    const pendingPresentation = { operation, sessionId: message.sessionId };
    this.pendingPresentation = pendingPresentation;
    let painted: boolean;
    try {
      // A view that belongs to a route in progress (guidance, a warning,
      // arrival) never takes the glass from a page the wearer opened; the
      // pages that ask the wearer something (place, mode, preview, error) do.
      painted = isRouteProgressView(candidate.navigation) && this.hud.updateNavigation
        ? await this.hud.updateNavigation(candidate.navigation, statusOverride)
        : await this.hud.showNavigation(candidate.navigation, statusOverride);
    } finally {
      if (this.pendingPresentation === pendingPresentation) {
        this.pendingPresentation = undefined;
      }
    }
    if (
      signal?.aborted
      || lifecycleEpoch !== this.lifecycleEpoch
      || operation !== this.presentationOperation
    ) return false;
    this.sendViewAck(message, painted ? 'bridge_acknowledged' : 'rejected');
    this.recordCue(candidate.navigation.view, painted);
    if (!painted) return false;
    this.current = candidate;
    return true;
  }

  clear(sessionId?: string): boolean {
    if (!this.current) return false;
    if (sessionId !== undefined && this.current.sessionId !== sessionId) return false;
    this.lifecycleEpoch += 1;
    this.current = undefined;
    return true;
  }

  async installRoute(message: NavigationRouteMessage): Promise<boolean> {
    const installEpoch = this.lifecycleEpoch;
    const prior = this.route;
    if (
      prior?.message.sessionId === message.sessionId
      && (
        message.generation < prior.message.generation
        || (message.routeId === prior.message.routeId && message.revision <= prior.message.revision)
      )
    ) {
      this.sendRouteAck(message, 'rejected');
      return false;
    }
    const phoneFixes = message.phoneFixes === true;
    if (!phoneFixes && !this.location) {
      this.sendRouteAck(message, 'rejected');
      return false;
    }

    let tracker: NavigationProgressTracker;
    try {
      tracker = new NavigationProgressTracker(message.plan);
    } catch {
      this.sendRouteAck(message, 'rejected');
      return false;
    }
    const route: ActiveRoute = {
      message,
      tracker,
      phoneFixes,
      sequence: 0,
      rerouteRequested: false,
      pausedForReroute: false,
      pendingProgress: [],
      progressRendering: false,
      repaintRequested: false,
      reportedFixShape: false,
    };
    if (phoneFixes) {
      if (prior && !prior.phoneFixes) {
        void this.location?.setNavigationConsumer(undefined).catch(() => false);
      }
      if (installEpoch !== this.lifecycleEpoch) return false;
      this.route = route;
      this.sendRouteAck(message, 'installed');
      return true;
    }
    let started = false;
    try {
      started = await this.location!.setNavigationConsumer((fix, receivedAt) => {
        this.ingestFix(route, fix, receivedAt);
      });
    } catch {
      started = false;
    }
    if (installEpoch !== this.lifecycleEpoch) {
      if (started) await this.location!.setNavigationConsumer(undefined).catch(() => false);
      return false;
    }
    if (!started) {
      this.sendRouteAck(message, 'rejected');
      return false;
    }
    this.route = route;
    this.sendRouteAck(message, 'installed');
    return true;
  }

  acceptExternalFix(msg: NavigationFixMessage): void {
    const route = this.route;
    if (!route || route.message.sessionId !== msg.sessionId || route.pausedForReroute) return;
    if (!Number.isFinite(msg.latitude) || !Number.isFinite(msg.longitude)) return;
    this.ingestFix(route, msg, this.clock());
  }

  async resumeToGlass(sessionId: string): Promise<boolean> {
    let model: NavigationHudModel | undefined;
    let requestId: string | undefined;
    let generation: number | undefined;
    const route = this.route;
    if (route?.message.sessionId === sessionId) {
      if (route.lastProgress) {
        model = this.progressModel(route, route.lastProgress.state, route.lastProgress.fix);
      } else if (this.current?.sessionId === sessionId) {
        model = this.cloneMessage(this.current).navigation;
      } else {
        const firstStep = route.message.plan.steps[0]!;
        const lastStep = route.message.plan.steps[route.message.plan.steps.length - 1]!;
        model = this.progressModel(route, {
          kind: 'guidance',
          stepId: firstStep.id,
          progressM: 0,
          crossTrackM: 0,
          maneuver: firstStep.maneuver,
          instruction: firstStep.instruction,
          maneuverDistanceM: Math.round(Math.max(0, firstStep.distanceAlongRouteM)),
          remainingDistanceM: Math.round(Math.max(0, lastStep.distanceAlongRouteM)),
        });
      }
      requestId = this.current?.sessionId === sessionId
        ? this.current.requestId
        : route.message.requestId;
      generation = this.current?.sessionId === sessionId
        ? this.current.generation
        : route.message.generation;
    } else if (this.current?.sessionId === sessionId) {
      const cloned = this.cloneMessage(this.current);
      model = cloned.navigation;
      requestId = cloned.requestId;
      generation = cloned.generation;
    }
    if (!model || requestId === undefined || generation === undefined) return false;
    const lifecycleEpoch = this.lifecycleEpoch;
    const operation = ++this.presentationOperation;
    const painted = await this.hud.showNavigation(model);
    if (!painted || lifecycleEpoch !== this.lifecycleEpoch || operation !== this.presentationOperation) {
      return false;
    }
    this.current = {
      kind: 'navigation.view',
      sessionId,
      requestId,
      generation,
      navigation: model,
    };
    return true;
  }

  private ingestFix(route: ActiveRoute, fix: NavigationFix, receivedAt: number): void {
    if (this.route !== route || route.pausedForReroute) return;
    const timestamp = locationTimestampMs(fix.timestamp, receivedAt);
    const safeFix: NavigationFix = {
      latitude: fix.latitude,
      longitude: fix.longitude,
      ...(Number.isFinite(fix.accuracy) ? { accuracy: fix.accuracy } : {}),
      ...(Number.isFinite(fix.heading) ? { heading: fix.heading } : {}),
      ...(Number.isFinite(fix.speed) ? { speed: fix.speed } : {}),
      ...(timestamp !== undefined ? { timestamp } : {}),
    };
    if (!route.reportedFixShape) {
      // The shape of the host's fixes, once per route and without the
      // position: which optional fields it fills decides whether the
      // tracker trusts, ages or rejects every fix that follows.
      route.reportedFixShape = true;
      console.info('[joat] navigation fix shape', {
        accuracy: Number.isFinite(fix.accuracy),
        heading: Number.isFinite(fix.heading),
        speed: Number.isFinite(fix.speed),
        timestamp: fix.timestamp === undefined ? 'absent'
          : timestamp === Math.round(fix.timestamp as number) ? 'milliseconds' : 'seconds',
        ageMs: timestamp === undefined ? undefined : receivedAt - timestamp,
      });
    }
    const state = route.tracker.accept(safeFix, receivedAt);
    this.enqueueProgress(route, state, safeFix);
  }

  async requestOrigin(
    message: Extract<DownlinkMessage, { kind: 'navigation.origin.request' }>,
    signal?: AbortSignal,
  ): Promise<boolean> {
    const lifecycleEpoch = this.lifecycleEpoch;
    const operation = ++this.originOperation;
    if (!this.location) {
      this.sendOriginFailure(message);
      return false;
    }
    let origin: Awaited<ReturnType<NavigationLocationSession['requestNavigationOrigin']>>;
    try {
      origin = await this.location.requestNavigationOrigin();
    } catch {
      if (
        signal?.aborted
        || lifecycleEpoch !== this.lifecycleEpoch
        || operation !== this.originOperation
      ) return false;
      this.sendOriginFailure(message);
      return false;
    }
    if (
      signal?.aborted
      || lifecycleEpoch !== this.lifecycleEpoch
      || operation !== this.originOperation
    ) return false;
    if (!origin || !Number.isFinite(origin.latitude) || !Number.isFinite(origin.longitude)) {
      this.sendOriginFailure(message);
      return false;
    }
    this.link.sendNavigation({
      kind: 'navigation.origin',
      sessionId: message.sessionId,
      requestId: message.requestId,
      generation: message.generation,
      latitude: origin.latitude,
      longitude: origin.longitude,
      ...(Number.isFinite(origin.accuracy) ? { accuracy: origin.accuracy } : {}),
    });
    return true;
  }

  private sendViewAck(
    message: NavigationViewMessage,
    outcome: 'bridge_acknowledged' | 'rejected',
  ): void {
    this.link.sendNavigation({
      kind: 'navigation.view.ack',
      sessionId: message.sessionId,
      requestId: message.requestId,
      generation: message.generation,
      outcome,
    });
  }

  private sendOriginFailure(
    message: Extract<DownlinkMessage, { kind: 'navigation.origin.request' }>,
  ): void {
    this.link.sendNavigation({
      kind: 'navigation.origin.failed',
      sessionId: message.sessionId,
      requestId: message.requestId,
      generation: message.generation,
      reason: 'unavailable',
    });
  }

  async stopRoute(sessionId: string): Promise<boolean> {
    const route = this.route;
    const ownsRoute = route?.message.sessionId === sessionId;
    const ownsView = this.current?.sessionId === sessionId;
    const ownsPendingView = this.pendingPresentation?.sessionId === sessionId;
    if (!ownsRoute && !ownsView && !ownsPendingView) return false;
    const visibleOnGlass = this.hud.isNavigationVisible ? this.hud.isNavigationVisible() : true;
    this.lifecycleEpoch += 1;
    if (ownsRoute && route) {
      route.pendingProgress.length = 0;
      this.route = undefined;
      if (!route.phoneFixes) {
        await this.location?.setNavigationConsumer(undefined).catch(() => false);
      }
    }
    if (ownsRoute || ownsView) this.hud.navigationEnded?.();
    if (ownsView) this.current = undefined;
    if (ownsView || ownsPendingView) this.recordCue('stopped', true);
    // The caller uses this result to decide whether to restore Overview. An
    // old route may be stopped after a newer session or menu already owns the
    // HUD; never let that stop hide the surface on the glass.
    return (ownsView && visibleOnGlass) || ownsPendingView;
  }

  /** Link loss revokes the Mac session that authorized navigation. Keep no
   * tracker, high-rate location request, or stale interactive HUD locally. */
  async stopAll(): Promise<boolean> {
    // A frame already crossing the four-tile bridge can finish after this
    // revocation. Report it as HUD ownership so the caller queues Overview
    // behind that frame even though input/current was never committed.
    const hadView = this.current !== undefined || this.pendingPresentation !== undefined;
    const hadRoute = this.route !== undefined;
    this.lifecycleEpoch += 1;
    this.current = undefined;
    if (this.route) this.route.pendingProgress.length = 0;
    this.route = undefined;
    if (hadRoute || this.location) {
      await this.location?.setNavigationConsumer(undefined).catch(() => false);
    }
    return hadView;
  }

  handleClick(): boolean {
    const current = this.current;
    if (!current) return false;
    const navigation = current.navigation;
    if (navigation.interaction === 'none') return true;

    switch (navigation.view) {
      case 'destination':
        this.send('confirm_destination');
        break;
      case 'repair':
        this.sendChoice('select_destination');
        break;
      case 'mode':
        this.sendChoice('select_mode');
        break;
      case 'preview':
        this.send('start');
        break;
      case 'arrived':
        this.send('finish');
        break;
      case 'error':
        this.send('retry');
        break;
      default:
        break;
    }
    return true;
  }

  /**
   * The left-temple double press is the one explicit on-glasses escape hatch
   * while navigation owns the HUD. Keep the current frame bound until the
   * Mac responds with navigation.stop, so another gesture cannot act on a
   * restored surface ahead of that terminal message.
   */
  handleCancel(): boolean {
    if (!this.current) return false;
    this.send('cancel');
    return true;
  }

  async handleScroll(direction: 'up' | 'down'): Promise<boolean> {
    const current = this.current;
    if (!current) return false;
    const navigation = current.navigation;
    if (navigation.interaction === 'none') return true;

    if (navigation.view === 'repair' || navigation.view === 'mode') {
      const delta = direction === 'up' ? -1 : 1;
      const maximum = Math.max(0, navigation.options.length - 1);
      const selectedIndex = Math.max(0, Math.min(maximum, navigation.selectedIndex + delta));
      if (selectedIndex === navigation.selectedIndex) return true;
      const next = {
        ...navigation,
        options: [...navigation.options],
        ...(navigation.choiceIds ? { choiceIds: [...navigation.choiceIds] } : {}),
        selectedIndex,
      };
      if (await this.hud.showNavigation(next) && this.current === current) {
        current.navigation = next;
      }
      return true;
    }

    if (navigation.view === 'destination') {
      this.send('retry_destination');
    } else if (navigation.view === 'preview') {
      this.send(direction === 'up' ? 'change_mode' : 'change_destination');
    } else if (navigation.view === 'error') {
      this.send('cancel');
    }
    return true;
  }

  private sendChoice(action: 'select_destination' | 'select_mode'): void {
    const current = this.current;
    if (!current) return;
    const choiceId = current.navigation.choiceIds?.[current.navigation.selectedIndex];
    if (!choiceId) return;
    this.send(action, choiceId);
  }

  private send(action: NavigationAction, choiceId?: string): void {
    const current = this.current;
    if (!current) return;
    this.link.sendNavigation({
      kind: 'navigation.action',
      sessionId: current.sessionId,
      requestId: current.requestId,
      generation: current.generation,
      action,
      ...(choiceId ? { choiceId } : {}),
    });
  }

  private sendRouteAck(
    message: NavigationRouteMessage,
    outcome: 'installed' | 'rejected',
  ): void {
    this.link.sendNavigation({
      kind: 'navigation.route.ack',
      sessionId: message.sessionId,
      routeId: message.routeId,
      revision: message.revision,
      outcome,
    });
  }

  /**
   * The SDK can deliver a fresh phone fix each second while one four-tile G2
   * frame is still crossing BLE. Queueing every intermediate position makes
   * the wearer see where they were rather than where they are. Collapse only
   * consecutive guidance-distance changes; safety warnings and every kind
   * transition remain visible in their original order.
   */
  private enqueueProgress(
    route: ActiveRoute,
    state: NavigationProgressState,
    fix: NavigationFix,
  ): void {
    const pending = route.pendingProgress;
    const tail = pending[pending.length - 1];
    if (state.kind === 'guidance' && tail?.state.kind === 'guidance') {
      pending[pending.length - 1] = { state, fix };
    } else {
      pending.push({ state, fix });
    }
    if (route.progressRendering) return;
    route.progressRendering = true;
    this.progressQueue = this.drainProgress(route).catch(() => undefined);
  }

  private async drainProgress(route: ActiveRoute): Promise<void> {
    try {
      while (this.route === route && !route.pausedForReroute) {
        const pending = route.pendingProgress.shift();
        if (!pending) return;
        await this.renderProgress(route, pending.state, pending.fix);
      }
    } finally {
      route.progressRendering = false;
    }
  }

  private async renderProgress(
    route: ActiveRoute,
    state: NavigationProgressState,
    fix: NavigationFix,
  ): Promise<void> {
    if (this.route !== route) return;
    const signature = state.kind === 'guidance'
      ? `${state.kind}:${state.stepId}:${state.maneuver}:${formatNavigationDistance(state.maneuverDistanceM)}`
      : state.kind === 'uncertain'
        ? `${state.kind}:${state.reason}`
        : state.kind;
    const semanticChange = signature !== route.lastSignature;
    const now = this.clock();
    // A position-only repaint: the wearer moved visibly since the last
    // painted frame, and that frame is at least a second old. Only the
    // guidance page has a dot to move; the warnings and arrival are text.
    const moved = state.kind === 'guidance'
      && (route.lastPaintedFix === undefined || distanceM(route.lastPaintedFix, fix) >= REPAINT_MIN_MOVE_M);
    const paced = now - (route.lastPaintAt ?? Number.NEGATIVE_INFINITY) >= REPAINT_MIN_INTERVAL_MS;
    if (!semanticChange && !route.repaintRequested && !(moved && paced)) return;
    if (route.pausedForReroute) return;
    route.repaintRequested = false;
    const navigation = this.progressModel(route, state, fix);
    const painted = this.hud.updateNavigation
      ? await this.hud.updateNavigation(navigation)
      : await this.hud.showNavigation(navigation);
    if (this.route !== route) return;
    if (semanticChange) this.recordCue(navigation.view, painted);
    if (!painted) return;
    route.lastSignature = signature;
    route.lastPaintAt = this.clock();
    route.lastPaintedFix = fix;
    route.lastProgress = { state, fix };
    this.current = {
      kind: 'navigation.view',
      sessionId: route.message.sessionId,
      requestId: route.message.requestId,
      generation: route.message.generation,
      navigation,
    };
    // The Mac hears about STATES, not positions: a position-only repaint
    // sends nothing, which is the privacy boundary ARCHITECTURE.md draws.
    if (!semanticChange) return;
    route.sequence += 1;
    this.link.sendNavigation({
      kind: 'navigation.state',
      sessionId: route.message.sessionId,
      routeId: route.message.routeId,
      revision: route.message.revision,
      sequence: route.sequence,
      state: state.kind,
      ...(state.kind === 'guidance' ? {
        maneuver: state.maneuver,
        maneuverDistanceM: state.maneuverDistanceM,
        ...(state.instruction.trim() ? { instruction: state.instruction.trim().slice(0, 128) } : {}),
        stepId: state.stepId,
        remainingDistanceM: state.remainingDistanceM,
        ...(navigation.etaMinutes !== undefined ? { etaMinutes: navigation.etaMinutes } : {}),
      } : {}),
    });
    if (state.kind === 'off_route' && !route.rerouteRequested) {
      route.rerouteRequested = true;
      route.pausedForReroute = true;
      route.pendingProgress.length = 0;
      this.link.sendNavigation({
        kind: 'navigation.reroute.request',
        sessionId: route.message.sessionId,
        routeId: route.message.routeId,
        baseRevision: route.message.revision,
        requestId: `${route.message.routeId}:${route.message.revision}:reroute`,
        latitude: fix.latitude,
        longitude: fix.longitude,
      });
    }
    if (state.kind === 'arrived' && !route.phoneFixes) {
      await this.location?.setNavigationConsumer(undefined);
    }
  }

  /**
   * A stray `navigation.map` raster tile. Ignored: since 2026-11-23 the
   * guidance pane is a route schematic drawn from the route and the live
   * fix alone, so a phone or Mac that still sends street tiles costs one
   * dropped message — no decode, no repaint, no state.
   */
  async installMapPanel(_message: NavigationMapMessage): Promise<void> {
    return undefined;
  }

  private progressModel(
    route: ActiveRoute,
    state: NavigationProgressState,
    fix?: NavigationFix,
  ): NavigationHudModel {
    const mode = route.message.plan.mode;
    // The schematic marks the maneuver the wearer is heading for, and
    // labels it with the street the instruction names.
    const nextStep = state.kind === 'guidance'
      ? route.message.plan.steps.find((step) => step.id === state.stepId)
      : undefined;
    const base = {
      interaction: 'none' as const,
      options: [] as readonly string[],
      selectedIndex: 0,
      mode,
      // The polyline the Mac already sent is the map. Carrying it into the
      // HUD model costs nothing on the wire and turns the guidance frame
      // from a line of text into an actual picture of the route.
      map: {
        geometry: route.message.plan.geometry,
        ...(fix ? { position: { latitude: fix.latitude, longitude: fix.longitude } } : {}),
        ...(fix && Number.isFinite(fix.heading) ? { heading: fix.heading } : {}),
        ...(fix && Number.isFinite(fix.speed) ? { speed: fix.speed } : {}),
        ...('progressM' in state ? { progressM: state.progressM } : {}),
        ...(nextStep ? {
          maneuverAlongM: nextStep.distanceAlongRouteM,
          ...((label) => (label ? { maneuverLabel: label } : {}))(navigationStreetLabel(nextStep.instruction)),
        } : {}),
      },
    };
    if (state.kind === 'guidance') {
      const total = state.progressM + state.remainingDistanceM;
      return {
        ...base,
        view: 'guidance',
        primary: maneuverTitle(state.maneuver),
        secondary: state.instruction,
        hint: `Then continue · ${formatNavigationDistance(state.maneuverDistanceM)} · double-left stops`,
        maneuver: state.maneuver,
        maneuverDistanceM: state.maneuverDistanceM,
        remainingDistanceM: state.remainingDistanceM,
        etaMinutes: Math.max(1, Math.round(
          route.message.etaMinutes * state.remainingDistanceM / Math.max(1, total),
        )),
        progress: state.progressM / Math.max(1, total),
      };
    }
    if (state.kind === 'uncertain') {
      return {
        ...base,
        view: 'uncertain',
        primary: 'Finding your position',
        secondary: state.reason === 'accuracy' ? 'GPS accuracy is low' : 'Route match is uncertain',
        hint: 'Pause safely · double-left stops',
      };
    }
    if (state.kind === 'wrong_way') {
      return {
        ...base,
        view: 'wrong_way',
        primary: 'Wrong direction',
        secondary: 'Turn around when safe',
        hint: 'Route resumes · double-left stops',
        maneuver: 'u_turn',
        remainingDistanceM: state.remainingDistanceM,
      };
    }
    if (state.kind === 'off_route') {
      return {
        ...base,
        view: 'off_route',
        primary: 'Off route',
        secondary: 'Stop when safe',
        hint: 'Rerouting · double-left stops',
      };
    }
    return {
      ...base,
      interaction: 'stationary',
      view: 'arrived',
      primary: 'You have arrived',
      secondary: route.message.destinationLabel,
      hint: 'tap: finish',
      maneuver: 'arrive',
      remainingDistanceM: 0,
      etaMinutes: 0,
      progress: 1,
    };
  }

  /** Observational only: the Mac assigns receipt time after the bridge accepts
   * this fixed semantic state. It deliberately receives no HUD copy or route
   * data, and never decides whether a frame or route can proceed. */
  private recordCue(cue: NavigationCue, painted: boolean): void {
    this.diagnostics?.record({
      type: 'navigation_cue',
      cue,
      outcome: painted ? 'success' : 'failed',
    });
  }

  private cloneMessage(message: NavigationViewMessage): NavigationViewMessage {
    return {
      ...message,
      navigation: {
        ...message.navigation,
        options: [...message.navigation.options],
        ...(message.navigation.choiceIds
          ? { choiceIds: [...message.navigation.choiceIds] }
          : {}),
      },
    };
  }
}

function maneuverTitle(maneuver: NavigationHudModel['maneuver']): string {
  switch (maneuver) {
    case 'turn_left': return 'Turn left';
    case 'turn_right': return 'Turn right';
    case 'slight_left': return 'Keep left';
    case 'slight_right': return 'Keep right';
    case 'u_turn': return 'Make a U-turn';
    case 'arrive': return 'Arrive';
    case 'depart': return 'Start route';
    default: return 'Continue';
  }
}
