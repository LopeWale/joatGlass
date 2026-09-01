import type { HudPresenter } from './HudPresenter';
import type {
  NavigationHudModel,
  NavigationMapPanelModel,
  NavigationRoadsModel,
} from './NavigationHud';
import { formatNavigationDistance } from './NavigationHud';
import {
  NavigationProgressTracker,
  type NavigationFix,
  type NavigationProgressState,
} from './NavigationProgress';
import type { LocationSessionManager } from './LocationSessionManager';
import type { G2DiagnosticTrace, NavigationCue } from './G2DiagnosticTrace';
import type {
  DownlinkMessage,
  NavigationAction,
  UplinkMessage,
} from './types';

type NavigationViewMessage = Extract<DownlinkMessage, { kind: 'navigation.view' }>;
type NavigationRouteMessage = Extract<DownlinkMessage, { kind: 'navigation.route' }>;
type NavigationMapMessage = Extract<DownlinkMessage, { kind: 'navigation.map' }>;
type NavigationRoadsMessage = Extract<DownlinkMessage, { kind: 'navigation.roads' }>;

/** Decode a Mac-rendered map tile. Isolated so a headless test can supply
 * its own decoder, and so a host without `Image` degrades to no map rather
 * than throwing inside the downlink handler. */
async function decodePngDataUrl(source: string): Promise<CanvasImageSource> {
  if (typeof Image === 'undefined') throw new Error('no image decoder');
  const image = new Image();
  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error('map decode failed'));
    image.src = source;
  });
  return image;
}
type NavigationUplink = Extract<UplinkMessage, {
  kind: 'navigation.action' | 'navigation.route.ack' | 'navigation.state'
    | 'navigation.reroute.request' | 'navigation.origin'
    | 'navigation.view.ack' | 'navigation.origin.failed';
}>;

interface NavigationHudSink {
  showNavigation(navigation: NavigationHudModel, statusOverride?: string): Promise<boolean>;
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
  /** Mac-rendered street surroundings for this route, once decoded. */
  mapPanel?: NavigationMapPanelModel;
  /** The road network JOAT draws itself, names included. */
  roads?: NavigationRoadsModel;
}

/**
 * Owns navigation input while a generation-bound navigation view is visible.
 * User-facing labels never select behavior; phase plus stable choice IDs do.
 */
export class NavigationController {
  private current?: NavigationViewMessage;
  private route?: ActiveRoute;
  /** Streets that arrived before their route did: the Mac fetches the map
   * as soon as it has an origin, which is before it has a route to send. */
  private pendingMapPanel?: { sessionId: string; panel: NavigationMapPanelModel };
  /** Roads that arrived before their route did. */
  private pendingRoads?: { sessionId: string; roads: NavigationRoadsModel };
  private progressQueue: Promise<void> = Promise.resolve();
  private lifecycleEpoch = 0;
  private presentationOperation = 0;
  private pendingPresentation?: { operation: number; sessionId: string };
  private originOperation = 0;

  constructor(
    private readonly hud: NavigationHudSink | Pick<HudPresenter, 'showNavigation'>,
    private readonly link: NavigationLink,
    private readonly location?: NavigationLocationSession,
    private readonly diagnostics?: G2DiagnosticTrace,
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
      painted = await this.hud.showNavigation(candidate.navigation, statusOverride);
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
    if (!this.location) {
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
    const pending = this.pendingMapPanel;
    const route: ActiveRoute = {
      message,
      tracker,
      sequence: 0,
      rerouteRequested: false,
      pausedForReroute: false,
      pendingProgress: [],
      progressRendering: false,
      // Adopt streets that arrived while this route was still being fetched.
      ...(pending?.sessionId === message.sessionId ? { mapPanel: pending.panel } : {}),
      ...(this.pendingRoads?.sessionId === message.sessionId
        ? { roads: this.pendingRoads.roads } : {}),
    };
    let started = false;
    try {
      started = await this.location.setNavigationConsumer((fix, receivedAt) => {
        if (this.route !== route || route.pausedForReroute) return;
        const safeFix: NavigationFix = {
          latitude: fix.latitude,
          longitude: fix.longitude,
          ...(Number.isFinite(fix.accuracy) ? { accuracy: fix.accuracy } : {}),
          ...(Number.isFinite(fix.heading) ? { heading: fix.heading } : {}),
          ...(Number.isFinite(fix.speed) ? { speed: fix.speed } : {}),
          ...(Number.isFinite(fix.timestamp) ? { timestamp: fix.timestamp } : {}),
        };
        const state = route.tracker.accept(safeFix, receivedAt);
        this.enqueueProgress(route, state, safeFix);
      });
    } catch {
      started = false;
    }
    if (installEpoch !== this.lifecycleEpoch) {
      if (started) await this.location.setNavigationConsumer(undefined).catch(() => false);
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
    this.lifecycleEpoch += 1;
    if (ownsRoute && route) {
      route.pendingProgress.length = 0;
      this.route = undefined;
      await this.location?.setNavigationConsumer(undefined).catch(() => false);
    }
    if (ownsView) this.current = undefined;
    if (ownsView || ownsPendingView) this.recordCue('stopped', true);
    // The caller uses this result to decide whether to restore Overview. An
    // old route may be stopped after a newer session already owns the HUD;
    // never let that stale stop hide the newer navigation view.
    return ownsView || ownsPendingView;
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
      ? `${state.kind}:${state.maneuver}:${formatNavigationDistance(state.maneuverDistanceM)}`
      : state.kind === 'uncertain'
        ? `${state.kind}:${state.reason}`
        : state.kind;
    if (signature === route.lastSignature) return;
    if (route.pausedForReroute) return;
    const navigation = this.progressModel(route, state, fix);
    const painted = await this.hud.showNavigation(navigation);
    if (this.route !== route) return;
    this.recordCue(navigation.view, painted);
    if (!painted) return;
    route.lastSignature = signature;
    this.current = {
      kind: 'navigation.view',
      sessionId: route.message.sessionId,
      requestId: route.message.requestId,
      generation: route.message.generation,
      navigation,
    };
    route.sequence += 1;
    this.link.sendNavigation({
      kind: 'navigation.state',
      sessionId: route.message.sessionId,
      routeId: route.message.routeId,
      revision: route.message.revision,
      sequence: route.sequence,
      state: state.kind,
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
    if (state.kind === 'arrived') {
      await this.location?.setNavigationConsumer(undefined);
    }
  }

  /**
   * Adopt the Mac-rendered surroundings for this session.
   *
   * Decoding happens once, here, rather than on every painted frame: the
   * renderer runs inside the BLE frame budget and must never wait on an
   * image decode. A failed decode is silent — guidance keeps working with
   * the route's bare shape, which is strictly better than a black pane.
   */
  /** Adopt the road network around the wearer.
   *
   * Same ordering trap as the map panel: the Mac fetches these the moment
   * it has an origin, before it has a route to attach them to, so an
   * early arrival is held rather than dropped. */
  installRoads(message: NavigationRoadsMessage): void {
    const roads: NavigationRoadsModel = {
      latitude: message.latitude,
      longitude: message.longitude,
      roads: message.roads.map((road) => ({
        name: road.name,
        weight: road.weight,
        points: road.points,
      })),
    };
    const route = this.route;
    if (route && route.message.sessionId === message.sessionId) {
      route.roads = roads;
      return;
    }
    this.pendingRoads = { sessionId: message.sessionId, roads };
  }

  async installMapPanel(
    message: NavigationMapMessage,
    decode: (source: string) => Promise<CanvasImageSource> = decodePngDataUrl,
  ): Promise<void> {
    // The Mac fetches the streets as soon as it has the wearer's origin —
    // which is BEFORE it has a route to send. Requiring an installed route
    // here silently dropped almost every tile, so guidance kept falling
    // back to the bare polyline. Hold it for the route that is coming.
    let image: CanvasImageSource;
    try {
      image = await decode(`data:image/png;base64,${message.png}`);
    } catch {
      console.warn('[joat] navigation map panel failed to decode');
      return;
    }
    const panel: NavigationMapPanelModel = {
      latitude: message.latitude,
      longitude: message.longitude,
      zoom: message.zoom,
      width: message.width,
      height: message.height,
      image,
    };
    const route = this.route;
    if (route && route.message.sessionId === message.sessionId) {
      route.mapPanel = panel;
      return;
    }
    this.pendingMapPanel = { sessionId: message.sessionId, panel };
  }

  private progressModel(
    route: ActiveRoute,
    state: NavigationProgressState,
    fix?: NavigationFix,
  ): NavigationHudModel {
    const mode = route.message.plan.mode;
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
        ...(route.mapPanel ? { panel: route.mapPanel } : {}),
        ...(route.roads ? { roads: route.roads } : {}),
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
