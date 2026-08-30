import { AudioInputSource, OsEventTypeList } from '@evenrealities/even_hub_sdk';
import { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import { MinimalHudPresenter } from './MinimalHudPresenter';
import { HudCanvasRenderer } from './HudCanvasRenderer';
import { encodeGray8TilePng, HudVisualSurface } from './HudVisualSurface';
import {
  activateVisualHudPage,
  initializeHudPage,
  restoreCompatibleHudPage,
  restoreMinimalHudPage,
  selectHudFirstLight,
} from './HudPageBootstrap';
import { Chrome } from './Chrome';
import { TOKEN_KEY, handlePairingOffer, loadToken } from './Pairing';
import { LocationSessionManager } from './LocationSessionManager';
import { MacLink } from './MacLink';
import { AudioStreamer } from './AudioStreamer';
import { ImuMonitor } from './ImuMonitor';
import { EventRouter, type LifecycleHandler } from './EventRouter';
import { DeviceMonitor } from './DeviceMonitor';
import { Settings } from './Settings';
import { CapabilityMenu } from './CapabilityMenu';
import { ContainerId, type DownlinkMessage, type ProductivityHudModel } from './types';
import {
  BrowserHudPreview,
  VISUAL_CONFIRMATION_PROMPT,
  brainstormVisualFixture,
  isBrainstormVisualState,
  isProductivityVisualState,
  runVisualConfirmationFixture,
  visualListFixture,
  type ProductivityVisualState,
  type BrainstormVisualState,
} from './VisualFixtures';
import { MacG2DiagnosticTrace, type G2TraceEvent } from './G2DiagnosticTrace';
import {
  isNavigationVisualState,
  navigationVisualFixture,
  type NavigationVisualState,
} from './NavigationHud';
import { NavigationController } from './NavigationController';
import { BrainstormController } from './BrainstormController';
import { deliverAfterConfirmationDismissal } from './ConfirmationCommit';
import {
  replaySyntheticAudioFixture,
  syntheticAudioFixtureUrl,
} from './SyntheticAudioFixture';

/**
 * Comma-separated candidate brain URLs — every host must be an exact-match
 * domain in app.json or the Hub blocks it silently. MacLink rotates through
 * them, so the brain can run on ANY whitelisted Mac without a plugin rebuild.
 */
const MAC_WS_URLS = ((): readonly string[] => {
  const parsed = (import.meta.env.VITE_MAC_WS_URL ?? '')
    .split(',')
    .map((u) => u.trim())
    .filter((u) => u.length > 0);
  // Fall back AFTER filtering: an empty or whitespace-only env var survives
  // `??` and would otherwise leave zero candidates and abort boot.
  return parsed.length > 0 ? parsed : ['wss://mac.local:8787/glasses'];
})();

type VisualState =
  | 'overview'
  | 'sidebar'
  | 'more'
  | 'confirmation'
  | ProductivityVisualState
  | ProductivityLifecycleVisualState
  | BrainstormVisualState
  | NavigationVisualState;

/** Development-only lenses gate for the native Calendar/Reminders screens.
 * Each state uses the same presenter and canvas renderer as a downlink from
 * the Mac bridge; it exists so the official simulator can prove pixels before
 * a package is put on the glasses. */
const PRODUCTIVITY_LIFECYCLE_VISUAL_STATES = [
  'calendar-review',
  'calendar-verifying',
  'calendar-saved',
  'calendar-failed',
  'calendar-check',
  'calendar-cancelled',
  'calendar-not-saved',
  'reminders-review',
  'reminders-verifying',
  'reminders-saved',
  'reminders-failed',
  'reminders-check',
  'reminders-cancelled',
  'reminders-not-saved',
  'reminders-completed',
  'reminders-due',
] as const;

type ProductivityLifecycleVisualState =
  (typeof PRODUCTIVITY_LIFECYCLE_VISUAL_STATES)[number];

function isProductivityLifecycleVisualState(
  value: string | null,
): value is ProductivityLifecycleVisualState {
  return PRODUCTIVITY_LIFECYCLE_VISUAL_STATES.some((state) => state === value);
}

const VISUAL_STATE = ((): VisualState | undefined => {
  if (!import.meta.env.DEV) return undefined;
  const requested = new URLSearchParams(window.location.search).get('joatVisual');
  return requested === 'overview' || requested === 'sidebar' || requested === 'more'
    || requested === 'confirmation' || isProductivityVisualState(requested)
    || isProductivityLifecycleVisualState(requested)
    || isBrainstormVisualState(requested)
    || (requested !== null && isNavigationVisualState(requested))
    ? requested
    : undefined;
})();

const BROWSER_VISUAL_PREVIEW = import.meta.env.DEV
  && new URLSearchParams(window.location.search).get('joatPreview') === 'browser';

/** Development-only acceptance hook. Unlike `joatVisual`, this keeps the
 * normal authenticated Mac link alive and merely opens the same sidebar a
 * left-temple double press would open. It lets the official simulator prove
 * a live capability request even though it hardcodes right-temple input. */
const LIVE_MENU_ACCEPTANCE = import.meta.env.DEV
  && new URLSearchParams(window.location.search).get('joatTestOpenMenu') === '1';

/** Webview status panel (index.html). Absent ids are a no-op by design. */
function setPanel(id: string, text: string): void {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

async function boot(): Promise<void> {
  if (VISUAL_STATE && BROWSER_VISUAL_PREVIEW) {
    const renderer = new HudCanvasRenderer();
    const preview = new BrowserHudPreview(renderer);
    const previewClient = new BridgeClient();
    const hud = new HudPresenter(previewClient, new HudPageBuilder(), preview);
    const previewLink = new MacLink({ urls: MAC_WS_URLS, token: loadToken() });
    const menu = new CapabilityMenu(
      hud,
      new Settings(),
      previewLink,
    );
    const navigation = new NavigationController(hud, previewLink);
    await renderVisualState(hud, menu, undefined, VISUAL_STATE, navigation);
    return;
  }
  const client = new BridgeClient();
  const layouts = new HudPageBuilder();
  // Bridge FIRST: the pairing token prefers HOST storage (the
  // WebView's localStorage is origin-scoped, so dev-server and packed
  // builds held separate tokens and every switch forced a manual re-pair;
  // host storage is one bucket for both).
  await client.init();
  let token = loadToken();
  try {
    const hosted = await client.require().getLocalStorage(TOKEN_KEY);
    if (hosted) token = hosted;
  } catch {
    // Older host: WebView storage remains the fallback.
  }
  const link = new MacLink({ urls: MAC_WS_URLS, token });
  // Disabled by default. Only the local Mac shell can enable this support
  // capture, and the gateway independently redacts every event it accepts.
  const g2Diagnostics = new MacG2DiagnosticTrace(link);
  // This tiny in-memory receipt is collected before the Mac can opt in, but
  // never leaves the plugin until the explicit support-capture control frame
  // arrives. It has no screen, location, device, or wearer data.
  const firstLightReceipt: G2TraceEvent[] = [];
  let firstLightReceiptDelivered = false;
  // The Even host exposes one unkeyed location stream. Keep one owner that
  // switches between the existing ambient contract and an explicit Gate-0
  // high-accuracy probe instead of racing independent start/stop callers.
  const location = new LocationSessionManager(client, link, g2Diagnostics);
  let navigationRef: NavigationController | undefined;
  let hudRef: HudPresenter | undefined;
  let audioRef: AudioStreamer | undefined;
  let imuRef: ImuMonitor | undefined;

  // A token the Mac no longer honours must not outlive the session, or
  // every relaunch dials with it and is refused again.
  link.onStaleToken(() => {
    try {
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      // Storage unavailable — the in-memory drop already unblocked us.
    }
    void client.require().setLocalStorage(TOKEN_KEY, '').catch(() => {});
  });

  setPanel('p-url', link.activeUrl);
  // A reconnected socket is a fresh session on the Mac's side — without a new
  // hello the brain never learns who connected and never greets. announce() is
  // set once device info exists; the guard covers the first pre-boot 'connected'.
  let announce: (() => void) | undefined;
  // Declared early: the link/mic callbacks below are registered before the
  // page exists, and must not paint into a page that is not there yet.
  let chromeRef: Chrome | undefined;
  let brainstormRef: BrainstormController | undefined;
  link.onState((state) => {
    setPanel('p-link', state);
    chromeRef?.setLink(state);
    setPanel('p-url', link.activeUrl); // failover rotation, made visible
    if (state === 'connected') announce?.();
    else {
      // A disconnected or replacement brain no longer authorizes either
      // opt-in diagnostic path. A fresh authenticated session will send its
      // complete true/false state again.
      g2Diagnostics.setEnabled(false);
      firstLightReceiptDelivered = false;
      void audioRef?.stop();
      void imuRef?.stop();
      void location.revokeRemoteControl();
      // A control gesture must never cross a MacLink generation boundary.
      brainstormRef?.clear();
      void navigationRef?.stopAll().then((hadView) => {
        if (
          hadView
          && link.linkState !== 'connected'
          && !navigationRef?.isActive()
        ) void hudRef?.showDefault();
      });
    }
  });

  // Create the product's four-tile G2 page first. If the native host rejects
  // it, initializeHudPage has already rebuilt the durable text fallback.
  const hudBoot = await initializeHudPage(client, layouts);
  const activeLayouts = layouts.withContextualMenu(hudBoot.contextualMenu);
  const fallbackSurface = hudBoot.surface === 'visual' ? 'text' : hudBoot.surface;
  if (hudBoot.surface !== 'visual' && !hudBoot.contextualMenu) {
    console.warn(
      '[joat] HUD compatibility mode: text, contextual menu off',
    );
  }

  // The modern four-tile HUD is the product surface. Its container rebuild and
  // first complete frame must both acknowledge before selection; otherwise
  // restore the durable text compatibility page instead of black-screening.
  const firstLight = await selectHudFirstLight({
    activateSurface: () => hudBoot.surface === 'visual'
      ? Promise.resolve('visual' as const)
      : activateVisualHudPage(
        client,
        activeLayouts,
        fallbackSurface,
        (accepted) => firstLightReceipt.push({
          type: 'hud_surface',
          stage: 'visual_rebuild',
          surface: 'visual',
          outcome: accepted ? 'success' : 'failed',
        }),
      ),
    restoreFallback: () => restoreCompatibleHudPage(
      client,
      activeLayouts,
      fallbackSurface,
    ),
    restoreMinimal: () => restoreMinimalHudPage(client, activeLayouts),
    createVisual: () => new HudPresenter(
      client,
      activeLayouts,
      new HudVisualSurface(client, new HudCanvasRenderer(), g2Diagnostics, encodeGray8TilePng),
      g2Diagnostics,
    ),
    createText: () => new HudPresenter(client, activeLayouts, undefined, g2Diagnostics),
    createMinimal: () => new MinimalHudPresenter(client, activeLayouts),
    onVisualFirstFrame: (painted) => firstLightReceipt.push({
      type: 'hud_surface',
      stage: 'first_frame',
      surface: 'visual',
      outcome: painted ? 'success' : 'failed',
    }),
  });
  firstLightReceipt.push({
    type: 'hud_surface',
    stage: 'selected',
    surface: firstLight.surface,
    outcome: 'success',
  });
  const hud = firstLight.hud;
  hudRef = hud;
  if (firstLight.surface === 'text') {
    console.warn('[joat] visual HUD unavailable; using text compatibility surface');
  } else if (firstLight.surface === 'minimal') {
    console.warn('[joat] rich HUD layouts unavailable; using live minimal recovery surface');
  }
  const audio = new AudioStreamer(client, link);
  const imu = new ImuMonitor(client, link);
  audioRef = audio;
  imuRef = imu;
  const settings = new Settings();
  settings.load();
  setPanel('p-policy', settings.resumePolicy);
  settings.onChange((policy) => setPanel('p-policy', policy));
  const menu = new CapabilityMenu(hud, settings, link);
  const navigation = new NavigationController(hud, link, location, g2Diagnostics);
  const brainstorm = new BrainstormController(link);
  brainstormRef = brainstorm;
  navigationRef = navigation;
  menu.micIsOpen = () => audio.isOpen;
  // Mic truth leaves the phone on every change — the Mac's pill used to
  // latch "streaming" at hello and never verify.
  audio.onState = (open) => {
    setPanel('p-mic', open ? 'streaming' : 'off');
    chromeRef?.setMic(open);
    link.send({ kind: 'device', connect: 'connected', mic: open });
  };

  const lifecycle: LifecycleHandler = {
    onForegroundEnter: () => {
      location.setForeground(true);
      // The host may have paused delivery while backgrounded; repaint so the
      // wearer isn't looking at a stale frame.
      void hud.setStatus(audio.isOpen ? 'JOAT · listening' : 'JOAT · mic off');
    },
    onForegroundExit: () => {
      location.setForeground(false);
      // Nothing to flush: Settings writes through on every change, so there is
      // no dirty state to lose. Deliberately a no-op rather than invented work.
      // If buffered state ever lands here, this is where it gets persisted.
    },
    onExit: (reason) => {
      // Terminal. Stop the hardware: a mic still streaming after the wearer
      // exited is a privacy bug, not a leak of a few frames.
      console.info(`[joat] exiting (${reason})`);
      void teardown();
    },
  };

  new EventRouter(
    client,
    link,
    audio,
    imu,
    menu,
    lifecycle,
    g2Diagnostics,
    navigation,
    brainstorm,
    hud,
  ).attach();

  // Official-simulator visual gate. This is DEV-only and drives the same
  // production presenter/menu/event methods as the real gesture and Mac paths;
  // it does not ship in the packed plugin or introduce an alternate renderer.
  if (VISUAL_STATE) {
    await renderVisualState(hud, menu, imu, VISUAL_STATE, navigation);
    setPanel('p-status', `visual: ${VISUAL_STATE}`);
    return;
  }

  const devices = new DeviceMonitor(client, link, audio, imu, hud, settings);
  devices.onBattery = (level, charging) => chromeRef?.setBattery(level, charging);
  devices.attach();

  link.onMessage(async (msg: DownlinkMessage, context) => {
    const ensureCurrent = (): void => {
      if (context.signal.aborted) {
        throw new Error(`stale downlink generation ${context.generation}`);
      }
    };
    const awaitCurrent = async <T>(operation: Promise<T>): Promise<T> => {
      const result = await operation;
      ensureCurrent();
      return result;
    };
    ensureCurrent();
    switch (msg.kind) {
      case 'g2.diagnostic.control':
        g2Diagnostics.setEnabled(msg.enabled);
        if (msg.enabled && !firstLightReceiptDelivered) {
          firstLightReceipt.forEach((event) => g2Diagnostics.record(event));
          firstLightReceiptDelivered = true;
        }
        break;
      case 'location.probe.control':
        await awaitCurrent(location.setProbeEnabled(msg.enabled, context.signal));
        break;
      case 'navigation.view':
        brainstorm.clear();
        await awaitCurrent(navigation.present(msg, undefined, context.signal));
        break;
      case 'brainstorm.view':
        await awaitCurrent(menu.beginLiveSession('brainstorm'));
        if (await awaitCurrent(hud.showBrainstorm(msg.brainstorm))) {
          brainstorm.present(msg.brainstorm);
        }
        break;
      case 'productivity.view':
        brainstorm.clear();
        await awaitCurrent(menu.presentProductivity(msg.productivity));
        break;
      case 'navigation.origin.request':
        await awaitCurrent(navigation.requestOrigin(msg, context.signal));
        break;
      case 'navigation.route':
        await awaitCurrent(navigation.installRoute(msg));
        break;
      case 'navigation.stop':
        brainstorm.clear();
        if (await awaitCurrent(navigation.stopRoute(msg.sessionId))) {
          await awaitCurrent(hud.showDefault());
        }
        break;
      case 'hud.text':
        // Status-bar writes become the STATE WORD, not the whole line: the
        // bar has one owner (Chrome), so the clock and battery survive what
        // the Mac has to say.
        if (msg.containerId === ContainerId.StatusBar) {
          chromeRef?.setMacState(msg.text);
          break;
        }
        // append streams (debate turns, token-by-token replies) without a
        // full repaint; plain hud.text replaces the container.
        if (msg.append) await awaitCurrent(hud.appendText(msg.text, msg.containerId));
        else {
          await awaitCurrent(
            hud.showText(msg.text, msg.containerId, msg.textColor, msg.contentKind),
          );
        }
        break;
      case 'assistant.overlay':
        if (msg.operation === 'replace' && msg.text) {
          await awaitCurrent(hud.showAssistantOverlay(msg.text));
        } else if (msg.operation === 'append' && msg.text) {
          await awaitCurrent(hud.showAssistantOverlay(msg.text, true));
        } else if (msg.operation === 'settle' && msg.dismissAfterMs !== undefined) {
          hud.settleAssistantOverlay(msg.dismissAfterMs);
        } else if (msg.operation === 'dismiss') {
          await awaitCurrent(hud.dismissAssistantOverlay());
        }
        break;
      case 'hud.list':
        if (await awaitCurrent(menu.presentList(
          msg.title,
          msg.items,
          msg.requestId,
          msg.selectable ?? false,
          msg.hint,
          msg.planningGrid,
        ))) {
          break;
        }
        await awaitCurrent(menu.presentLifecycle(msg.title, msg.items, msg.hint));
        break;
      case 'hud.rebuild':
        if (msg.reason === 'brainstorm') {
          await awaitCurrent(menu.beginLiveSession('brainstorm'));
        } else {
          brainstorm.clear();
          await awaitCurrent(hud.showDefault());
        }
        break;
      case 'audio.control':
        if (msg.open) {
          const ok = await awaitCurrent(
            audio.start(
              msg.source === 'phone' ? AudioInputSource.Phone : AudioInputSource.Glasses,
              context.signal,
            ),
          );
          setPanel('p-mic', ok ? 'streaming' : 'unavailable');
        } else {
          await awaitCurrent(audio.stop());
          setPanel('p-mic', 'off');
        }
        break;
      case 'imu.control':
        msg.open
          ? await awaitCurrent(imu.start(undefined, context.signal))
          : await awaitCurrent(imu.stop());
        break;
      case 'imu.trace':
        imu.setTrace(msg.enabled);
        break;
      case 'confirm.request': {
        const rendered = hud.showConfirmation(msg.prompt, msg.actionId);
        imu.prepare(msg.actionId, (accepted) => {
          void deliverAfterConfirmationDismissal({
            accepted,
            dismiss: () => hud.dismissConfirmation(msg.actionId),
            isAborted: () => context.signal.aborted,
            deliver: () => link.send({
              kind: 'confirm',
              actionId: msg.actionId,
              accepted,
              challengeId: msg.challengeId,
              sessionId: msg.sessionId,
              payloadDigest: msg.payloadDigest,
              previewDigest: msg.previewDigest,
            }),
          }).catch((err: unknown) => {
            if (!context.signal.aborted) {
              console.error('[joat] confirmation dismissal failed:', err);
            }
          });
        }, context.signal);
        let visible = false;
        try {
          visible = await awaitCurrent(rendered);
        } catch (err) {
          if (context.signal.aborted) return;
          console.error('[joat] confirmation render failed:', err);
        }
        if (visible) {
          // Content-free acceptance synchronization for the official desktop
          // simulator. Vite removes this branch from packed builds.
          if (import.meta.env.DEV) console.info('[joat] acceptance confirmation-visible');
          imu.activate(msg.actionId, 8000);
        } else imu.cancel(msg.actionId);
        break;
      }
      case 'pairing.set':
        handlePairingOffer(msg.token, link, (tok) => {
          void client.require().setLocalStorage(TOKEN_KEY, tok).catch(() => {});
        });
        break;
      case 'policy.set':
        // Mac-side config path; the wearer's on-glasses menu is the other.
        settings.setResumePolicy(msg.resume);
        link.send({ kind: 'policy', resume: settings.resumePolicy });
        break;
      case 'shutdown':
        await teardown();
        await client.require().shutDownPageContainer(msg.exitMode ?? 1);
        break;
    }
  });

  link.connect();

  if (LIVE_MENU_ACCEPTANCE) {
    await menu.toggle();
    console.info('[joat] live menu acceptance opened');
  }

  setPanel('p-status', 'connecting');

  // Tolerate a stalled/absent device info — both fields already default, and
  // the wearer would rather have a live HUD than an accurate serial number.
  const device = await Promise.race([
    client.require().getDeviceInfo(),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), 3000)),
  ]).catch(() => null);
  // Host account name grounds "the wearer's name" with zero configuration
  //. Best-effort: older hosts reject the call.
  // Some simulator/older-host bridges advertise getUserInfo yet never settle
  // the promise. Identity is decorative; it must never strand the required
  // hello handshake that lets the Mac paint every glasses surface.
  const user = await Promise.race([
    client.require().getUserInfo(),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), 3000)),
  ]).catch(() => null);
  announce = () => {
    link.send({
      kind: 'hello',
      sn: device?.sn ?? 'unknown',
      model: device?.model ?? 'unknown',
      launchSource: client.getLaunchSource() ?? 'unknown',
      ...(user?.name ? { userName: user.name } : {}),
    });
    // Announce the persisted policy so the Mac's session logic starts in sync.
    link.send({ kind: 'policy', resume: settings.resumePolicy });
  };
  announce();

  // Both starts degrade to false on hosts missing the capability (the desktop
  // simulator has no IMU at all) — boot must survive either being absent.
  // The boot-time device status was fetched and thrown away:
  // glasses sitting in the case had their mic opened anyway. Only an
  // EXPLICIT not-worn skips — unknown must not silence a working mic.
  const worn = device?.status?.isWearing;
  let micStatus: string;
  if (worn === false) {
    micStatus = 'JOAT · mic off (glasses not worn)';
    setPanel('p-mic', 'off (not worn)');
  } else {
    let micOk = await audio.start(AudioInputSource.Glasses);
    let label = 'JOAT · listening';
    if (!micOk) {
      // The fallback ear: the phone mic, NAMED on the status
      // line — a silent source switch on a privacy surface is worse than
      // no fallback.
      micOk = await audio.start(AudioInputSource.Phone);
      label = 'JOAT · listening (phone mic)';
    }
    micStatus = micOk ? label : 'JOAT · mic unavailable';
  }
  await imu.start();

  // Deterministic official-simulator voice gate. Vite replaces DEV with false
  // and removes this branch from packed builds. The resolver independently
  // requires a loopback page and a tokenized same-origin fixture path.
  if (import.meta.env.DEV) {
    const fixtureUrl = syntheticAudioFixtureUrl(window.location.href);
    if (fixtureUrl !== undefined) {
      const batches = await replaySyntheticAudioFixture(audio, fixtureUrl);
      console.info(`[joat] synthetic audio fixture replayed (${batches} batches)`);
    }
  }

  // Plugin-owned chrome: clock, link, mic, battery, and the gesture hint —
  // painted from state the PHONE has, so the HUD keeps informing the wearer
  // through a Mac outage instead of going blank.
  const chrome = new Chrome(hud);
  chromeRef = chrome;
  // A fast local gateway can connect while device/mic discovery is still in
  // flight. Replay the retained link truth instead of leaving Chrome at its
  // constructor default of `connecting` for the rest of the session.
  chrome.setLink(link.linkState);
  chrome.setMic(audio.isOpen);
  chrome.setBattery(device?.status?.batteryLevel, device?.status?.isCharging === true);
  chrome.start();
  if (micStatus.includes('phone mic')) {
    await hud.showText('Using the phone mic — glasses mic unavailable.', ContainerId.Main);
  } else if (worn === false) {
    await hud.showText('Put the glasses on to start listening.', ContainerId.Main);
  }

  // Wearer position -> Mac: weather-without-asking, and the
  // brain stops answering timezone questions from the Mac's point of view.
  void location.start();
  setPanel('p-status', audio.isOpen ? 'listening' : 'mic off');
  setPanel('p-mic', audio.isOpen ? 'streaming' : 'off');

  async function teardown(): Promise<void> {
    await audio.stop();
    imu.dispose();
    await imu.stop();
    await location.shutdown();
    link.close();
    client.dispose();
  }

  window.addEventListener('beforeunload', () => { void teardown(); });
}

async function renderVisualState(
  hud: HudPresenter,
  menu: CapabilityMenu,
  imu: ImuMonitor | undefined,
  state: VisualState,
  navigation?: NavigationController,
): Promise<void> {
  hud.setMicState(true);

  if (isNavigationVisualState(state)) {
    const simulatorStatus = '9:41       JOAT simulator no route       bat 87%';
    const fixture = navigationVisualFixture(state);
    if (navigation) {
      await navigation.present({
        kind: 'navigation.view',
        sessionId: 'simulator-no-route',
        requestId: `fixture-${state}`,
        generation: 1,
        navigation: fixture,
      }, simulatorStatus);
    } else {
      await hud.showNavigation(fixture, simulatorStatus);
    }
    return;
  }

  if (isBrainstormVisualState(state)) {
    await hud.showBrainstorm(
      brainstormVisualFixture(state),
      '9:41       JOAT brainstorm       bat 87%',
    );
    return;
  }

  await hud.showText('9:41       JOAT ready       bat 87%', ContainerId.StatusBar, 2);

  if (isProductivityLifecycleVisualState(state)) {
    if (state === 'calendar-review') {
      await hud.showConfirmation(
        'Create event “Design review”? Work · Sep 1, 2026 at 09:30 · 45m · America/Chicago',
        'fixture-calendar-review',
      );
      return;
    }
    if (state === 'reminders-review') {
      await hud.showConfirmation(
        'Create reminder + JOAT alert “Buy filters”? Personal · Sep 1, 2026 at 18:00 · America/Chicago',
        'fixture-reminders-review',
      );
      return;
    }
    await hud.showProductivity(productivityLifecycleFixture(state));
    return;
  }

  if (isProductivityVisualState(state)) {
    const fixture = visualListFixture(state);
    await hud.showList(fixture.items, {
      title: fixture.title,
      hint: fixture.hint,
      selectable: fixture.selectable,
    });
    return;
  }

  if (state === 'confirmation') {
    if (imu) await runVisualConfirmationFixture(hud, imu);
    else await hud.showConfirmation(VISUAL_CONFIRMATION_PROMPT, 'browser-visual-confirmation');
    return;
  }

  await hud.showText('NEXT\nDesign review · 4:30', ContainerId.Main, 4);
  await hud.showText(
    'LAST QUERY\nWhere is the nearest coffee shop?', ContainerId.Context, 2,
  );
  await hud.showText('2 notifications waiting', ContainerId.Tray, 2);
  await hud.showText('left x2 menu · long-press mic', ContainerId.Hint, 1);
  if (state === 'sidebar') {
    await menu.toggle();
  } else if (state === 'more') {
    await menu.toggle();
    for (let index = 0; index < 4; index += 1) {
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    }
    menu.handleClick();
  }
}

function productivityLifecycleFixture(
  state: Exclude<ProductivityLifecycleVisualState, 'calendar-review' | 'reminders-review'>,
): ProductivityHudModel {
  const calendar = state.startsWith('calendar-');
  const domain = calendar ? 'calendar' : 'reminders';
  const title = calendar ? 'Design review' : 'Buy filters';
  const nativeApp = calendar ? 'Calendar' : 'Reminders';
  // The production workflow returns to one live, mixed planner (not stale
  // app-specific lists) after every terminal status.
  const returnHint = 'left x2 Planning';
  switch (state) {
    case 'calendar-verifying':
    case 'reminders-verifying':
      return {
        domain, phase: 'verifying', title,
        primary: `Saving ${calendar ? 'calendar event' : 'reminder'}`,
        secondary: 'Checking native read-back', hint: 'Please wait',
      };
    case 'calendar-saved':
      return {
        domain, phase: 'saved', title,
        primary: 'Saved and verified', secondary: `${nativeApp} read-back matched`, hint: returnHint,
      };
    case 'reminders-saved':
      return {
        domain, phase: 'saved', title,
        primary: 'Saved and verified', secondary: 'Native alarm set · JOAT monitoring armed.', hint: returnHint,
      };
    case 'calendar-failed':
    case 'reminders-failed':
      return {
        domain, phase: 'failed', title,
        primary: 'Not saved', secondary: `No ${nativeApp.toLowerCase()} item was changed.`, hint: returnHint,
      };
    case 'calendar-check':
    case 'reminders-check':
      return {
        domain, phase: 'check', title,
        primary: `Check ${nativeApp}`, secondary: 'It may have changed. Do not retry.',
        hint: returnHint,
      };
    case 'calendar-cancelled':
    case 'calendar-not-saved':
    case 'reminders-cancelled':
    case 'reminders-not-saved':
      return {
        domain,
        phase: state.endsWith('cancelled') ? 'cancelled' : 'not_saved',
        title,
        primary: state.endsWith('cancelled') ? 'Cancelled' : 'Not saved',
        secondary: `No ${nativeApp.toLowerCase()} item was changed.`,
        hint: returnHint,
      };
    case 'reminders-completed':
      return {
        domain, phase: 'completed', title,
        primary: 'Completed and verified', secondary: 'Reminders read-back matched', hint: returnHint,
      };
    case 'reminders-due':
      return {
        domain, phase: 'due', title,
        primary: 'Due now', secondary: 'This reminder is still incomplete.', hint: returnHint,
      };
  }
}

void boot().catch((err) => {
  // Surface boot failures somewhere visible — the WebView has no console UI.
  document.body.textContent = `boot failed: ${err instanceof Error ? err.message : String(err)}`;
});
