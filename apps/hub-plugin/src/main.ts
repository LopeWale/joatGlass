import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import { MinimalHudPresenter } from './MinimalHudPresenter';
import { phoneTimeZone } from './PhoneTimeZone';
import { HudCanvasRenderer, usesActionPillLayout } from './HudCanvasRenderer';
import {
  encodeGray8TilePng,
  HudVisualSurface,
  PAINT_COALESCE_MS,
} from './HudVisualSurface';
import {
  activateVisualHudPage,
  initializeHudPage,
  restoreCompatibleHudPage,
  restoreMinimalHudPage,
  selectHudFirstLight,
} from './HudPageBootstrap';
import { Chrome } from './Chrome';
import {
  PAIRING_TOKENS_KEY,
  PREFERRED_BRAIN_KEY,
  TOKEN_KEY,
  decodePairingTokens,
  encodePairingTokens,
  handlePairingOffer,
  loadPairingTokens,
  loadToken,
  mergePairingTokens,
  migrateLegacyToken,
} from './Pairing';
import { onBackgroundRestore, setBackgroundState } from './BackgroundState';
import { LocationSessionManager } from './LocationSessionManager';
import { MacLink } from './MacLink';
import { AudioStreamer } from './AudioStreamer';
import { PromptedInputController } from './PromptedInputController';
import { ImuMonitor, imuReportPaceFromMs } from './ImuMonitor';
import { EventRouter, type LifecycleHandler } from './EventRouter';
import { DeviceMonitor } from './DeviceMonitor';
import { Settings } from './Settings';
import { PhoneControlPanel } from './PhoneControlPanel';
import { PhoneControlScreen } from './PhoneControlScreen';
import { Places, placesToWire } from './Places';
import { PlacesPanel } from './PlacesPanel';
import { CapabilityMenu, usesFirmwareList } from './CapabilityMenu';
import {
  CAPABILITIES,
  ContainerId,
  VOICE_REQUEST_PREFIX,
  type BrainstormHudModel,
  type CapabilityName,
  type DownlinkMessage,
  type ProductivityHudModel,
} from './types';
import {
  ASSISTANT_OVERLAY_VISUAL_TEXT,
  BrowserHudPreview,
  VISUAL_CONFIRMATION_PROMPT,
  brainstormVisualFixture,
  isBrainstormVisualState,
  isPanelVisualState,
  isProductivityVisualState,
  panelArticleFixture,
  panelVisualPushes,
  panelVisualTab,
  runVisualConfirmationFixture,
  visualListFixture,
  type PanelVisualState,
  type ProductivityVisualState,
  type BrainstormVisualState,
} from './VisualFixtures';
import { MacG2DiagnosticTrace, type G2TraceEvent } from './G2DiagnosticTrace';
import {
  isNavigationVisualState,
  navigationVisualFixture,
  type NavigationMapPanelModel,
  type NavigationVisualState,
} from './NavigationHud';
import { NavigationController } from './NavigationController';
import { BrainstormController } from './BrainstormController';
import { reminderDueAckFor } from './ReminderDueReceipt';
import { productivityViewAckFor } from './ProductivityViewReceipt';
import { confirmationDecisionWindowMs, deliverAfterConfirmationDismissal } from './ConfirmationCommit';
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
  //
  // The fallback must itself be whitelisted in app.json. This was
  // `wss://mac.local:8787/glasses`, a host that appears in no manifest —
  // and because the choice is made at runtime the literal ships in every
  // bundle whether or not it is ever dialled, so a packaged release carried
  // an origin its own manifest did not allow.
  return parsed.length > 0
    ? parsed
    : ['wss://laptop-mac.example-tailnet.ts.net/glasses'];
})();

/** Accept a remembered candidate only when it remains in this package's
 * manifest-allowlisted candidate set. Durable storage must never become an
 * alternate URL configuration channel. */
function rememberedBrain(raw: string | null | undefined): string | null {
  return raw !== null && raw !== undefined && MAC_WS_URLS.includes(raw) ? raw : null;
}

function loadRememberedBrain(): string | null {
  try {
    return rememberedBrain(localStorage.getItem(PREFERRED_BRAIN_KEY));
  } catch {
    return null;
  }
}

type VisualState =
  | 'overview'
  | 'live-concurrency'
  | 'confirmation'
  | 'assistant-conversation'
  | 'message-reply-question'
  | 'message-reply-confirm'
  | 'assistant-overlay'
  | 'nav-receipt'
  | 'notification-card'
  | ProductivityVisualState
  | ProductivityLifecycleVisualState
  | BrainstormVisualState
  | PanelVisualState
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
  return requested === 'overview'
    || requested === 'live-concurrency'
    || requested === 'confirmation' || requested === 'assistant-overlay'
    || requested === 'assistant-conversation'
    || requested === 'message-reply-question' || requested === 'message-reply-confirm'
    || requested === 'nav-receipt'
    || requested === 'notification-card'
    || isProductivityVisualState(requested)
    || isProductivityLifecycleVisualState(requested)
    || isBrainstormVisualState(requested)
    || isPanelVisualState(requested)
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
const LIVE_MENU_ACCEPTANCE = ((): CapabilityName | undefined => {
  if (!import.meta.env.DEV) return undefined;
  const requested = new URLSearchParams(window.location.search).get('joatTestOpenMenu');
  // `1` keeps the historical Planning entry; a capability name opens that
  // row, so an acceptance run can start on Messages without a sidebar.
  if (requested === '1') return 'Planning';
  return CAPABILITIES.find((name) => name === requested);
})();

/** DEV acceptance only: `?joatTestPromptMic=1` answers a question's
 * microphone as ready without opening one, so the official simulator (which
 * has no audio device) can show the question page long enough to be
 * photographed. The question still arrives, is acknowledged and is cancelled
 * over the real wire; only the capture it would arm is skipped. */
const LIVE_PROMPT_WITHOUT_MIC = import.meta.env.DEV
  && new URLSearchParams(window.location.search).get('joatTestPromptMic') === '1';

/** Webview status panel (index.html). Absent ids are a no-op by design. */
/** The phone page's two tabs: Connection (the existing form) and Places. */
function installPhoneTabs(): void {
  const tabs = Array.from(document.querySelectorAll<HTMLButtonElement>('nav.tabs [role="tab"]'));
  for (const tab of tabs) {
    tab.addEventListener('click', () => {
      for (const other of tabs) {
        const selected = other === tab;
        other.setAttribute('aria-selected', String(selected));
        const panel = document.getElementById(other.getAttribute('aria-controls') ?? '');
        if (panel) panel.hidden = !selected;
      }
    });
  }
}

function setPanel(id: string, text: string): void {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

async function boot(): Promise<void> {
  if (VISUAL_STATE && BROWSER_VISUAL_PREVIEW) {
    const renderer = new HudCanvasRenderer();
    const preview = new BrowserHudPreview(renderer);
    // The preview has no Even host. The presenter still rebuilds a page
    // whenever the glass would switch between the four tiles and a firmware
    // list (NEWS, SEARCH), so those calls must land somewhere that answers:
    // a host that accepts every rebuild and text write and draws nothing.
    // The frames the preview shows are the pixel pages only — a firmware
    // list page is the host's own font and cannot be previewed here.
    const previewClient = {
      require: () => ({
        rebuildPageContainer: async () => true,
        textContainerUpgrade: async () => true,
      }),
    } as unknown as BridgeClient;
    const hud = new HudPresenter(previewClient, new HudPageBuilder(), preview);
    const previewLink = new MacLink({
      urls: MAC_WS_URLS,
      tokens: migrateLegacyToken(loadPairingTokens(), loadToken(), MAC_WS_URLS[0]),
      preferredUrl: loadRememberedBrain(),
    });
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
  // Bridge FIRST: host storage mirrors the origin-scoped WebView storage, so
  // dev and packed builds can use the same approved Mac brains. Credentials
  // are a per-origin map: a pairing secret from Mac A must never be sent to
  // Mac B merely because both appear in this package's allowlist.
  await client.init();
  let tokens = loadPairingTokens();
  let preferredUrl = loadRememberedBrain();
  try {
    const [hostedTokensRaw, hostedPreferredRaw, hostedLegacy] = await Promise.all([
      client.require().getLocalStorage(PAIRING_TOKENS_KEY),
      client.require().getLocalStorage(PREFERRED_BRAIN_KEY),
      client.require().getLocalStorage(TOKEN_KEY),
    ]);
    // The Even host is the common persistence layer between an installed
    // package and its development webview, so let its valid per-origin map
    // win over a stale local mirror. No URL is accepted from storage.
    tokens = mergePairingTokens(decodePairingTokens(hostedTokensRaw), tokens);
    tokens = migrateLegacyToken(tokens, hostedLegacy, MAC_WS_URLS[0]);
    preferredUrl = preferredUrl ?? rememberedBrain(hostedPreferredRaw);
  } catch {
    // Older host: WebView storage remains the fallback.
  }
  tokens = migrateLegacyToken(tokens, loadToken(), MAC_WS_URLS[0]);
  const link = new MacLink({ urls: MAC_WS_URLS, tokens, preferredUrl });
  // Disabled by default. Only the local Mac shell can enable this support
  // capture, and the gateway independently redacts every event it accepts.
  const g2Diagnostics = new MacG2DiagnosticTrace(link);
  // This tiny in-memory receipt is collected before the Mac can opt in, but
  // never leaves the plugin until the explicit support-capture control frame
  // arrives. It has no screen, location, device, or wearer data.
  const firstLightReceipt: G2TraceEvent[] = [];
  let firstLightReceiptDelivered = false;
  // The Even host exposes one unkeyed iPhone-location stream. Keep one owner
  // that switches between ambient context, an explicit diagnostic probe, and
  // an active high-accuracy route instead of racing independent start/stop
  // callers.
  const location = new LocationSessionManager(client, link, g2Diagnostics);
  let navigationRef: NavigationController | undefined;
  let hudRef: HudPresenter | undefined;
  let audioRef: AudioStreamer | undefined;
  let imuRef: ImuMonitor | undefined;

  const persistPairingTokens = (next: Readonly<Record<string, string>>): void => {
    const encoded = encodePairingTokens(next);
    try {
      localStorage.setItem(PAIRING_TOKENS_KEY, encoded);
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      // Storage unavailable — MacLink's in-memory map still remains correct.
    }
    void client.require().setLocalStorage(PAIRING_TOKENS_KEY, encoded).catch(() => {});
    void client.require().setLocalStorage(TOKEN_KEY, '').catch(() => {});
  };

  // A token the Mac no longer honours must not outlive the session, but only
  // delete the one candidate that rejected it. The other home Macs retain
  // their independently approved credentials.
  link.onStaleToken((_origin, next) => {
    persistPairingTokens(next);
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
  let promptedInputRef: PromptedInputController | undefined;
  let menuRef: CapabilityMenu | undefined;
  let displayYielded = false;
  let exiting = false;
  let phonePanel: PhoneControlPanel | undefined;
  // The phone-side Brainstorm/Accounts screen. It only ever sends
  // `control.action`; everything it shows comes from the Mac.
  const controlScreen = new PhoneControlScreen({ send: (message) => link.send(message) });
  link.onState((state) => {
    controlScreen.setConnected(state === 'connected' && !link.wasReplaced);
    if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'link', state);
    setPanel('p-link', link.wasReplaced ? 'Another JOAT display connected. Use Reconnect to take over.' : state);
    phonePanel?.updateLink(state);
    if (link.wasReplaced) {
      displayYielded = true;
      chromeRef?.stop();
      client.suspendDisplay();
      hudRef?.suspendDisplay();
    } else if (state === 'connected' && displayYielded) {
      displayYielded = false;
      client.resumeDisplay();
      hudRef?.resumeDisplay();
      chromeRef?.start();
    }
    chromeRef?.setLink(state);
    setPanel('p-url', link.activeUrl); // failover rotation, made visible
    if (state === 'connected') {
      const active = link.activeUrl;
      try {
        localStorage.setItem(PREFERRED_BRAIN_KEY, active);
      } catch {
        // The active authenticated session remains usable without storage.
      }
      void client.require().setLocalStorage(PREFERRED_BRAIN_KEY, active).catch(() => {});
      announce?.();
    }
    else {
      promptedInputRef?.cancel('connection', false);
      menuRef?.retireTerminalOpening(false);
      menuRef?.retireBrainstormSurface();
      menuRef?.retireMessageUI(false);
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
      new HudVisualSurface(
        client,
        new HudCanvasRenderer(),
        g2Diagnostics,
        encodeGray8TilePng,
        {
          // A voice turn's chrome arrives as a burst of Mac messages; one
          // window renders them as one frame instead of shipping a tile twice.
          coalesceMs: PAINT_COALESCE_MS,
          // Tile calls are paced past the host's 100 ms hold window, so a
          // frame's last tile is never left waiting for the next frame.
          tileGapMs: 0,
        },
      ),
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
  const promptAudio: Pick<AudioStreamer, 'openPromptMicrophone' | 'activatePromptEpoch' | 'clearPromptEpoch'> = LIVE_PROMPT_WITHOUT_MIC
    ? {
      openPromptMicrophone: () => ({ ready: Promise.resolve(true), release: () => undefined }),
      activatePromptEpoch: (_epoch, acknowledge) => acknowledge(),
      clearPromptEpoch: () => undefined,
    }
    : audio;
  const promptedInput = new PromptedInputController({
    presenter: hud, audio: promptAudio, link,
    isOwnerCurrent: (message) => menuRef?.isMessagePromptCurrent(message) ?? false,
    onCancel: (message, reason) => {
      if (reason === 'back' && message.ownerKind === 'messages') {
        menuRef?.cancelMessageAction(message.ownerId);
      }
    },
  });
  promptedInputRef = promptedInput;
  audio.onPromptInvalidated = (promptId) => promptedInput.audioInvalidated(promptId);
  const imu = new ImuMonitor(client, link);
  audioRef = audio;
  imuRef = imu;
  const settings = new Settings();
  settings.load();
  setPanel('p-policy', settings.resumePolicy);
  settings.onChange((policy) => {
    setPanel('p-policy', policy);
    phonePanel?.updatePolicy(policy);
  });
  // The wearer's saved places: edited on the phone page, mirrored to the
  // Even host's storage, and sent whole to the Mac on every change and on
  // every connect, so "navigate home" resolves there.
  const places = new Places();
  places.load();
  places.onChange((list) => {
    link.send({ kind: 'places', places: placesToWire(list) });
  });
  new PlacesPanel({ places, currentLocation: () => location.requestNavigationOrigin() });
  installPhoneTabs();
  phonePanel = new PhoneControlPanel({
    link,
    urls: MAC_WS_URLS,
    settings,
    canReconnect: () => !exiting,
    selectBrain: async (url) => {
      if (exiting || !MAC_WS_URLS.includes(url)) return false;
      // Explicit phone migration retires every old actionable surface before
      // the next Mac can authorize input. Automatic reconnect is unchanged.
      link.close();
      menuRef?.releaseToLiveSession();
      chromeRef?.stop();
      hud.suspendDisplay();
      let stopTimer: ReturnType<typeof setTimeout> | undefined;
      const stopped = await Promise.race([
        Promise.all([audio.stop(), imu.stop(), navigationRef?.stopAll()]).then(() => true),
        new Promise<false>((resolve) => { stopTimer = setTimeout(() => resolve(false), 5000); }),
      ]).finally(() => { if (stopTimer !== undefined) clearTimeout(stopTimer); });
      if (!stopped || exiting) return false;
      hud.resumeDisplay();
      const cleared = await hud.showDefault();
      if (!cleared || exiting) return false;
      chromeRef?.start();
      const selected = link.selectUrl(url);
      if (selected) {
        // Remember user intent before authentication, including an offline Mac.
        // A stale host mirror must not replace this choice on the next launch.
        try { localStorage.setItem(PREFERRED_BRAIN_KEY, url); } catch { /* session selection still holds */ }
        void client.require().setLocalStorage(PREFERRED_BRAIN_KEY, url).catch(() => {});
      }
      return selected;
    },
    checkLocation: () => location.requestNavigationOrigin(),
    changePolicy: (policy) => {
      settings.setResumePolicy(policy);
      link.send({ kind: 'policy', resume: policy });
    },
  });
  // Durable resume policy: the Even host's storage survives Android WebView
  // kills and is shared across the dev/packed origins, unlike the browser
  // localStorage mirror. Best-effort and async — boot never waits on it.
  void settings.attachHostStorage({
    get: (key) => client.require().getLocalStorage(key),
    set: (key, value) => client.require().setLocalStorage(key, value),
  });
  void places.attachHostStorage({
    get: (key) => client.require().getLocalStorage(key),
    set: (key, value) => client.require().setLocalStorage(key, value),
  });
  const menu = new CapabilityMenu(hud, settings, link);
  menuRef = menu;
  const navigation = new NavigationController(hud, link, location, g2Diagnostics);
  const brainstorm = new BrainstormController(link);
  menu.onSurfaceDeparture = () => brainstorm.clear();
  brainstormRef = brainstorm;
  navigationRef = navigation;
  menu.micIsOpen = () => audio.isOpen;
  // Mic truth leaves the phone on every change — the Mac's pill used to
  // latch "streaming" at hello and never verify.
  audio.onState = (open, micPromptId) => {
    if (!open && micPromptId === undefined) promptedInput.cancel('microphone');
    setPanel('p-mic', open ? 'streaming' : 'off');
    chromeRef?.setMic(open);
    link.send({ kind: 'device', connect: 'connected', mic: open,
      micSource: audio.lastSource === AudioInputSource.Phone ? 'phone' : 'glasses',
      ...(micPromptId === undefined ? {} : { micPromptId }) });
  };

  const lifecycle: LifecycleHandler = {
    onContextualMenu: (open) => {
      menu.setContextualMenuOpen(open);
      brainstorm.setSuspended(open);
      // Closing firmware UI may clear its underlay. Restore the current
      // surface, including a capability chosen from this menu.
      if (!open && !exiting) void hud.repaintRestoredSurface();
    },
    onForegroundEnter: () => {
      if (exiting) return;
      link.resumeIfReplaced();
      location.setForeground(true);
      // The host may have cleared the page while we were behind its own UI —
      // the exit dialog's "No" was the measured case: the glasses stayed
      // BLACK because only the status line repainted (wearer report,
      // 2026-09-05). Repaint the whole restored surface, then the status.
      void hud.repaintRestoredSurface();
      void hud.setStatus(audio.isOpen ? 'JOAT · listening' : 'JOAT · mic off');
    },
    onForegroundExit: () => {
      promptedInput.cancel('background');
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

  // The open reply draft's "say it again", while its consent is up.
  let confirmationRetry: (() => boolean) | undefined;
  new EventRouter(
    client,
    link,
    audio,
    imu,
    menu,
    lifecycle,
    g2Diagnostics,
    {
      isActive: () => navigation.isActive() && hud.isNavigationVisible(),
      handleClick: () => navigation.handleClick(),
      handleScroll: direction => navigation.handleScroll(direction),
      handleCancel: () => navigation.handleCancel(),
    },
    brainstorm,
    hud,
    (direction) => hud.scrollTranscript(direction),
    promptedInput,
    () => confirmationRetry?.() ?? false,
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

  const terminalPreludeGuards = new WeakMap<DownlinkMessage, () => boolean>();
  const brainstormVisitGuards = new WeakMap<DownlinkMessage, () => boolean>();
  link.onMessageIngress((msg, context) => {
    if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'ingress', msg.kind);
    if (msg.kind === 'brainstorm.view' || (msg.kind === 'hud.rebuild' && msg.reason === 'brainstorm')) {
      brainstormVisitGuards.set(msg, menu.captureBrainstormVisit());
    }
    if (msg.kind === 'hud.list' && msg.selectable !== true
      && msg.title?.trim().toUpperCase() === 'AGENT TERMINAL'
      && msg.requestId?.startsWith(VOICE_REQUEST_PREFIX)) {
      terminalPreludeGuards.set(msg, menu.captureTerminalPreludeGuard());
    }
    if (import.meta.env.DEV && msg.kind === 'agent_terminal.view') {
      console.info('[blank-trace]', Date.now(), 'terminal-state', msg.terminal.state,
        msg.terminal.activity.length, msg.terminal.activity.includes('[!]'));
    }
    if (msg.kind === 'message.ui'
      && menu.observeMessageUI(msg, () => !context.signal.aborted)) {
      promptedInput.isActive();
    }
    promptedInput.observe(msg, context);
    const currentConfirmation = msg.kind === 'confirm.request'
      && (msg.messageUIActionId === undefined || menu.isMessageActionCurrent(msg.messageUIActionId));
    if (currentConfirmation || msg.kind === 'shutdown'
      || msg.kind === 'hud.rebuild'
      || (msg.kind === 'audio.control' && (!msg.open
        || (msg.source !== undefined && (
          msg.source === 'phone' ? AudioInputSource.Phone : AudioInputSource.Glasses
        ) !== audio.lastSource)))) {
      promptedInput.cancel('surface-transition');
    }
  });

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
      case 'message.ui':
        await awaitCurrent(menu.presentMessageUI(msg, () => !context.signal.aborted));
        break;
      case 'prompt.present':
      case 'prompt.cancel':
        await awaitCurrent(promptedInput.handle(msg, context));
        break;
      case 'prompt.partial':
        // Not awaited: the words so far must never hold the downlink queue
        // behind a card paint (the next partial supersedes this one).
        void promptedInput.partial(msg, context);
        break;
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
      case 'location.refresh':
        await awaitCurrent(location.requestAssistantLocation(msg.requestId, context.signal));
        break;
      case 'navigation.view':
        brainstorm.clear();
        // Navigation owns the lenses as one live session. Release any open
        // menu/capability state (and its armed response timeout) without an
        // extra Overview paint racing this frame on the BLE lane.
        menu.releaseToLiveSession();
        await awaitCurrent(navigation.present(msg, undefined, context.signal));
        break;
      case 'brainstorm.view': {
        // The WIRE dropped `screen` when the two features were split, but
        // the render model still keys on it — and every gesture check in
        // BrainstormController starts with `model.screen === 'brainstorm'`.
        // Passing the wire frame straight through left it undefined, so a
        // tap matched neither branch and did nothing at all. The
        // acceptance flow caught it; no unit test did, because they all
        // build the render model directly.
        const model: BrainstormHudModel = {
          ...msg.brainstorm,
          screen: 'brainstorm',
        };
        const visitCurrent = brainstormVisitGuards.get(msg);
        brainstormVisitGuards.delete(msg);
        const mayPaint = () => !context.signal.aborted
          && visitCurrent !== undefined && menu.mayPresentBrainstorm(visitCurrent);
        if (!mayPaint()) break;
        await awaitCurrent(menu.beginLiveSession('brainstorm'));
        if (mayPaint() && await awaitCurrent(hud.showBrainstorm(model, undefined, mayPaint)) && mayPaint()) {
          brainstorm.present(model);
        }
        break;
      }
      case 'agent_terminal.view': {
        // The WIRE is split — the Agent Terminal has its own frame kind and
        // no longer rides the Brainstorm one, which docs/SESSION_MODEL.md:74-75
        // forbids. The render model stays unified behind `screen`, so this
        // adapts one to the other in a single place instead of duplicating
        // the drawing code.
        const model: BrainstormHudModel = {
          screen: 'agent_terminal',
          state: msg.terminal.state,
          topic: msg.terminal.focus,
          collaborators: msg.terminal.lanes,
          activity: msg.terminal.activity,
          endpointHost: msg.terminal.endpointHost,
          terminalReason: msg.terminal.terminalReason,
          noToolsChecked: msg.terminal.noToolsChecked,
          approvals: msg.terminal.approvals,
          conversationId: msg.terminal.conversationId,
          messages: msg.terminal.messages?.map(message => ({...message})),
          historyTruncated: msg.terminal.historyTruncated,
          inputRequired: msg.terminal.inputRequired,
          taskRecovery: msg.terminal.taskRecovery,
        };
        const openingRequestId = msg.terminal.openingRequestId;
        const openingSelectionId = msg.terminal.openingSelectionId;
        if (openingRequestId !== undefined || openingSelectionId !== undefined) {
          if (openingRequestId === undefined || openingSelectionId === undefined) break;
          if (await awaitCurrent(menu.presentTerminalOpening(
            model, { requestId: openingRequestId, selectionId: openingSelectionId },
            () => !context.signal.aborted,
          ))) brainstorm.present(model);
          break;
        }
        if (!menu.mayPresentUnboundTerminal()) break;
        menu.retireTerminalOpening();
        menu.releaseToLiveSession();
        if (await awaitCurrent(hud.showBrainstorm(model))) {
          brainstorm.present(model);
        }
        break;
      }
      case 'productivity.view':
        brainstorm.clear();
        {
          const receipt = productivityViewAckFor(msg.productivity)
            ?? reminderDueAckFor(msg.productivity);
          if (navigation.isActive() && usesActionPillLayout(msg.productivity.phase)) {
            // A live route keeps the lenses: strip over it, no menu state,
            // no scheduled return that would replace the route with Planning.
            const shown = await awaitCurrent(hud.showProductivity(msg.productivity));
            if (shown && receipt) link.send(receipt);
            break;
          }
          await awaitCurrent(menu.presentProductivity(
            msg.productivity,
            receipt
              ? () => {
                // This is a current-client acceptance receipt only. If the
                // socket closes before this write, the Mac rejects the stale
                // generation/attempt after reconnect.
                link.send(receipt);
              }
              : undefined,
          ));
        }
        break;
      case 'navigation.origin.request':
        await awaitCurrent(navigation.requestOrigin(msg, context.signal));
        break;
      case 'navigation.route':
        await awaitCurrent(navigation.installRoute(msg));
        break;
      case 'hud.ambient':
        await awaitCurrent(hud.setAmbient({
          temperature: msg.temperature,
          condition: msg.condition,
          unread: msg.unread,
        }));
        break;
      case 'hud.notifications':
        // The unread list the notification card moves through.
        hud.acceptNotifications(Array.isArray(msg.cards) ? msg.cards : []);
        break;
      case 'hud.panel':
        // Cache first, so a swipe onto this tab paints from memory. Then:
        // a voice-initiated open expands the tab; a push for a tab whose
        // page is already on the glass repaints that page in place.
        await awaitCurrent(hud.acceptPanel(msg));
        if (msg.activate) {
          brainstorm.clear();
          await awaitCurrent(menu.openPanelTab(msg.tab, 0, false));
        } else {
          await awaitCurrent(menu.presentPanelContent(msg.tab));
        }
        break;
      case 'navigation.map':
        // Fire and forget: a map is an enhancement, and the downlink loop
        // must never wait on an image decode.
        void navigation.installMapPanel(msg);
        break;
      case 'navigation.stop':
        brainstorm.clear();
        if (await awaitCurrent(navigation.stopRoute(msg.sessionId))) {
          await awaitCurrent(hud.showDefault());
        }
        break;
      case 'hud.text':
        if (msg.containerId === ContainerId.Main && msg.contentKind === 'notification'
          && msg.text === '' && msg.traceId) {
          await awaitCurrent(hud.retireNotification(msg.traceId));
          break;
        }
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
            hud.showText(msg.text, msg.containerId, msg.textColor, msg.contentKind,
              msg.contentKind === 'notification' ? msg.traceId : undefined),
          );
        }
        break;
      case 'assistant.overlay':
        if (msg.operation === 'replace' && msg.text) {
          await awaitCurrent(hud.showAssistantOverlay(
            msg.text, false, msg.answersWearer === true, msg.presentation ?? 'answer',
          ));
        } else if (msg.operation === 'append' && msg.text) {
          await awaitCurrent(hud.showAssistantOverlay(
            msg.text, true, msg.answersWearer === true, msg.presentation ?? 'answer',
          ));
        } else if (msg.operation === 'settle' && msg.dismissAfterMs !== undefined) {
          hud.settleAssistantOverlay(msg.dismissAfterMs);
        } else if (msg.operation === 'dismiss') {
          await awaitCurrent(hud.dismissAssistantOverlay());
        }
        break;
      case 'assistant.conversation':
        if (msg.operation === 'replace') {
          await awaitCurrent(hud.showAssistantConversation(msg));
        } else {
          await awaitCurrent(hud.dismissAssistantConversation(
            msg.conversationId,
            msg.revision,
          ));
        }
        break;
      case 'hud.list': {
        const preludeCurrent = terminalPreludeGuards.get(msg);
        terminalPreludeGuards.delete(msg);
        if (preludeCurrent && !preludeCurrent()) break;
        // CapabilityMenu retires Brainstorm only after accepting this list's
        // owner. A stale response must not clear the current controller.
        if (await awaitCurrent(menu.presentList(
          msg.title,
          msg.items,
          msg.requestId,
          msg.selectable ?? false,
          msg.hint,
          msg.planningGrid,
          msg.contextLabel,
          msg.controlStartIndex,
          msg.planningEntries,
          msg.quickNotesPage,
          msg.readerPage,
          msg.planningNavigation,
          msg.pageControls,
        ))) {
          break;
        }
        await awaitCurrent(menu.presentLifecycle(msg.title, msg.items, msg.hint));
        break;
      }
      case 'hud.rebuild':
        if (msg.reason === 'brainstorm') {
          const visitCurrent = brainstormVisitGuards.get(msg);
          brainstormVisitGuards.delete(msg);
          if (visitCurrent && menu.claimBrainstormVisit(visitCurrent)) {
            await awaitCurrent(menu.beginLiveSession('brainstorm'));
          }
        } else {
          menu.retireBrainstormSurface();
          // A fresh brain is a fresh start: the resting screen reopens on
          // its first tab, the one that shows what that brain now says.
          hud.resetTab();
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
          ? await awaitCurrent(imu.start(imuReportPaceFromMs(msg.paceMs), context.signal))
          : await awaitCurrent(imu.stop());
        break;
      case 'imu.trace':
        imu.setTrace(msg.enabled);
        break;
      case 'confirm.request': {
        const ownerCurrent = () => !context.signal.aborted && (msg.messageUIActionId === undefined
          || menu.isMessageActionCurrent(msg.messageUIActionId));
        if (!ownerCurrent()) break;
        const rendered = hud.showConfirmation(msg.prompt, msg.actionId, msg.diagnosticKind,
          msg.messageUIActionId === undefined ? undefined : ownerCurrent, msg.purpose);
        if (msg.diagnosticKind === 'messages' && msg.messageUIActionId === undefined) {
          menu.stageLegacyReviewReturn(msg.actionId);
        }
        const messageUIActionId = msg.messageUIActionId;
        const retry = msg.diagnosticKind === 'messages' && messageUIActionId !== undefined
          ? () => {
            // Ask the Mac to say it again, then decline this draft: the Mac
            // asks afresh once the draft has ended, in either order.
            if (!ownerCurrent() || !menu.retryMessageAction(messageUIActionId)) return false;
            imu.declineByTap();
            return true;
          }
          : undefined;
        confirmationRetry = retry;
        imu.prepare(msg.actionId, (accepted) => {
          if (confirmationRetry === retry) confirmationRetry = undefined;
          void deliverAfterConfirmationDismissal({
            accepted,
            dismiss: () => hud.dismissConfirmation(msg.actionId),
            isAborted: () => !ownerCurrent(),
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
        }, context.signal, (outcome) => {
          if (msg.diagnosticKind === 'messages') {
            g2Diagnostics.record({ type: 'messages_confirmation', outcome });
          }
        });
        let visible = false;
        try {
          visible = await awaitCurrent(rendered);
        } catch (err) {
          if (context.signal.aborted) return;
          console.error('[joat] confirmation render failed:', err);
        }
        if (visible && ownerCurrent()) {
          // Content-free acceptance synchronization for the official desktop
          // simulator. Vite removes this branch from packed builds.
          if (import.meta.env.DEV) console.info('[joat] acceptance confirmation-visible');
          imu.activate(msg.actionId, confirmationDecisionWindowMs(msg.expiresAtMs));
        } else {
          imu.cancel(msg.actionId);
          if (msg.messageUIActionId !== undefined) await hud.dismissConfirmation(msg.actionId);
        }
        break;
      }
      case 'pairing.set':
        handlePairingOffer(msg.token, link, (encoded) => {
          try {
            localStorage.removeItem(TOKEN_KEY);
          } catch {
            // The v1 map was already durably written before this callback.
          }
          void client.require().setLocalStorage(PAIRING_TOKENS_KEY, encoded).catch(() => {});
          void client.require().setLocalStorage(TOKEN_KEY, '').catch(() => {});
        });
        break;
      case 'control.state':
        controlScreen.update(msg);
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
      // Read at announce time, not at module load: `announce` fires again on
      // every reconnect, so a wearer who landed while the link was down is
      // corrected by the reconnect itself.
      ...(phoneTimeZone() ? { timeZone: phoneTimeZone() } : {}),
    });
    // Announce the persisted policy so the Mac's session logic starts in sync,
    // and the saved places so a route to "home" resolves on this Mac.
    link.send({ kind: 'policy', resume: settings.resumePolicy });
    link.send({ kind: 'places', places: placesToWire(places.list()) });
    link.send({ kind: 'device', connect: 'connected', mic: audio.isOpen,
      micSource: audio.lastSource === AudioInputSource.Phone ? 'phone' : 'glasses' });
  };
  announce();

  // Both starts degrade to false on hosts missing the capability (the desktop
  // simulator has no IMU at all) — boot must survive either being absent.
  // The boot-time device status was fetched and thrown away:
  // glasses sitting in the case had their mic opened anyway. Only an
  // EXPLICIT not-worn skips — unknown must not silence a working mic.
  const worn = device?.status?.isWearing;
  let micStatus: string;
  if (displayYielded || exiting) {
    micStatus = 'JOAT · mic off';
  } else if (worn === false) {
    micStatus = 'JOAT · mic off (glasses not worn)';
    setPanel('p-mic', 'off (not worn)');
  } else {
    let micOk = await audio.start(AudioInputSource.Glasses);
    let label = 'JOAT · listening';
    if (!micOk && !displayYielded && !exiting) {
      // The fallback ear: the phone mic, NAMED on the status
      // line — a silent source switch on a privacy surface is worse than
      // no fallback.
      micOk = await audio.start(AudioInputSource.Phone);
      label = 'JOAT · listening (phone mic)';
    }
    micStatus = micOk ? label : 'JOAT · mic unavailable';
  }
  // No IMU at boot. Motion approves nothing (ImuMonitor: confirmation is a
  // deliberate tap), so a stream opened here did nothing but send ten reports
  // a second over Bluetooth and fill the diagnostics ring: the wearer's
  // export of 2026-10-07 held 512 motion reports and no taps. The Mac opens
  // it on purpose (imu.control) when it traces motion.

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
  if (displayYielded) {
    chrome.stop();
    hud.suspendDisplay();
  } else {
    chrome.start();
  }

  // Backgrounding the phone does not pause this page — the host snapshots it,
  // builds a NEW headless WebView, reloads this same URL, and replays the
  // snapshot. A plugin that registers nothing gets '{}' and reboots to its
  // initial state on every pocket-and-return, dumping the wearer back on
  // Overview mid-task. Register before the first restore can arrive.
  setBackgroundState('joat.hud', () => hud.exportBackgroundState());
  onBackgroundRestore('joat.hud', (saved) => {
    if (exiting) return;
    // The host restores the original foreground WebView after its headless
    // replacement has owned the connection. This explicit lifecycle event,
    // not a retry timer, permits one takeover. A second callback is inert.
    link.resumeIfReplaced();
    if (!hud.importBackgroundState(saved)) return;
    // Repaint ONCE, here — the reloaded page has already drawn its startup
    // frame, and the restored surface is what the wearer was actually
    // looking at before the phone went into their pocket.
    void hud.repaintRestoredSurface();
  });

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

  // The simulator cannot generate the physical left-double gesture. Open the
  // exact same menu only after the page, chrome, and Mac handshake have all
  // settled. The simulator also emits one late protobuf-zero startup press;
  // let that announcement drain before exposing a selectable row, otherwise
  // it can immediately activate Messages without any wearer input. Vite
  // strips this bounded acceptance-only delay from a package.
  if (LIVE_MENU_ACCEPTANCE) {
    await new Promise<void>((resolve) => setTimeout(resolve, 750));
    await menu.openFromContextualMenu(CAPABILITIES.indexOf(LIVE_MENU_ACCEPTANCE));
    console.info(`[joat] live menu acceptance opened=${menu.isOpen()}`);
  }

  async function teardown(): Promise<void> {
    if (exiting) return;
    exiting = true;
    chromeRef?.stop();
    promptedInput.cancel('exit');
    await audio.stop();
    imu.dispose();
    await imu.stop();
    await location.shutdown();
    link.close();
    client.dispose();
  }

  window.addEventListener('beforeunload', () => { void teardown(); });
}

/** Dev-fixture helper: decode the checked-in sample street panel. */
async function loadDevMapPanel(): Promise<NavigationMapPanelModel | undefined> {
  try {
    const image = new Image();
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error('sample map failed'));
      // Under __joat- so the release guard refuses any build that carries it.
      // Cache-bust: the WebView serves a stale sample happily, which made a
      // regenerated tile look like the renderer had ignored the change.
      image.src = `/__joat-dev-fixtures/dev-map-sample.png?v=${Date.now()}`;
    });
    return {
      latitude: 35.477809, longitude: -97.525642, zoom: 16,
      width: 284, height: 208, image,
    };
  } catch {
    return undefined;
  }
}

/**
 * Development only: three features at once, on a timeline the simulator
 * probe captures. A running Pomodoro and a route, then a conversation in
 * front of the route (the route keeps moving behind it, the header carries
 * the turn), then the conversation ends and the map returns at the newest
 * frame. Every step goes through the production presenter.
 */
async function renderLiveConcurrency(hud: HudPresenter): Promise<void> {
  const clock = '9:41';
  await hud.setStatusModel({ clock, link: 'connected', mic: true, battery: 87, charging: false });
  await hud.acceptPanel({
    kind: 'hud.panel', tab: 'pomodoro', revision: 1, rows: ['Pause', 'Reset'],
    pomodoro: {
      phase: 'focus', running: true, remainingMs: 12 * 60_000 - 1_000, lengthMs: 25 * 60_000,
      completed: 1, endsAtUnixMs: Date.now() + 12 * 60_000 - 1_000,
    },
  });
  const route = navigationVisualFixture('nav-guidance');
  if (route.map && !route.map.panel) {
    const sample = await loadDevMapPanel();
    if (sample) route.map = { ...route.map, panel: sample };
  }
  const step = (maneuverDistanceM: number) => ({ ...route, maneuver: 'turn_right' as const, maneuverDistanceM });
  await hud.showNavigation(step(240));
  console.info('[joat] concurrency stage=map');
  const later = (ms: number, work: () => Promise<unknown>) => new Promise<void>((resolve) => {
    setTimeout(() => { void work().finally(resolve); }, ms);
  });
  await later(6_000, async () => {
    await hud.showAssistantConversation({
      conversationId: 'walk-and-talk', revision: 1, state: 'ready',
      turns: [
        { role: 'user', text: 'What did Dele say about the gate?' },
        { role: 'assistant', text: 'Park on the left side of the building; the gate code is the same as last time.' },
      ],
      continueAfterMs: 120_000, earlierTurnsOmitted: false,
    });
    console.info('[joat] concurrency stage=conversation');
  });
  await later(5_000, async () => {
    await hud.updateNavigation(step(140));
    await hud.updateNavigation(step(80));
    console.info('[joat] concurrency stage=walking');
  });
  await later(5_000, async () => {
    await hud.requestAssistantConversationExit();
    await hud.requestAssistantConversationExit();
    console.info('[joat] concurrency stage=back');
  });
}

async function renderVisualState(
  hud: HudPresenter,
  menu: CapabilityMenu,
  imu: ImuMonitor | undefined,
  state: VisualState,
  navigation?: NavigationController,
): Promise<void> {
  hud.setMicState(true);
  // Chrome owns status composition in production and is started well after
  // this fixture gate returns. Without a composer here every setStatus (mic
  // toggle, link change) clobbered the whole line, so the clock and battery
  // vanished and the overview drew "--" for both — a simulator-only lie
  // about production. Compose like Chrome; keep the clock fixed so captures
  // stay byte-comparable.
  hud.onStatus = (text: string): void => {
    const macState = text.replace(/^JOAT\s*[·-]?\s*/i, '').trim();
    // Chrome.setMic couples the privacy icon to the state word. Without that
    // coupling the fixture drew "MIC OFF" beside a lit ON icon — a privacy
    // indicator contradicting the line next to it.
    if (/mic off/i.test(macState)) hud.setMicState(false);
    else if (/listening/i.test(macState)) hud.setMicState(true);
    void hud.showText(
      `9:41 JOAT ${macState || 'ready'} bat 87%`,
      ContainerId.StatusBar,
    );
  };

  // The header the glasses draw, from the same structured model Chrome
  // gives production: without it every fixture captured a placeholder
  // line no wearer ever sees.
  await hud.setStatusModel({ clock: '9:41', link: 'connected', mic: true, battery: 87, charging: false });

  if (state === 'live-concurrency') {
    await renderLiveConcurrency(hud);
    return;
  }

  if (isNavigationVisualState(state) || state === 'nav-receipt') {
    // READY on purpose. A degraded link outranks the page name in the spine
    // (drawShell), so a fixture that says "simulator no route" makes every
    // captured navigation frame read "JOAT · SIMULATOR NO ROUTE" instead of
    // "NAVIGATION" — a fixture artefact that looks exactly like a title bug
    // and cost a review cycle. Fixtures must show what the wearer sees.
    const simulatorStatus = '9:41       JOAT ready       bat 87%';
    const fixture = navigationVisualFixture(
      state === 'nav-receipt' ? 'nav-guidance' : state,
    );
    // Development only: load a real Mac-rendered street panel so the
    // simulator shows the same composite the wearer gets, not a bare line.
    if (fixture.map && !fixture.map.panel) {
      const sample = await loadDevMapPanel();
      if (sample) fixture.map = { ...fixture.map, panel: sample };
    }
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
    if (state === 'nav-receipt') {
      // Receipt strip over the live route; the route must stay visible.
      await hud.showProductivity(productivityLifecycleFixture('calendar-saved'));
    }
    return;
  }

  if (isBrainstormVisualState(state)) {
    await hud.showBrainstorm(
      brainstormVisualFixture(state),
      // A LINK status, not a feature name: the spine names the surface
      // itself now. This fixture used to hand both screens the string
      // "JOAT brainstorm", which is how the Agent Terminal came to be
      // captured with BRAINSTORM printed across its top.
      '9:41       JOAT READY       bat 87%',
    );
    return;
  }

  await hud.showText('9:41       JOAT ready       bat 87%', ContainerId.StatusBar, 2);

  // The notification card over Home (or `&joatNoteOver=news`, over the News
  // list), three unread cards, opened as Home's Notifications tab opens them:
  // no display timer, so a capture or a tap is never raced by it.
  if (state === 'notification-card') {
    await hud.setAmbient({ temperature: '72f', condition: 'clear', unread: 3 });
    for (const push of panelVisualPushes()) await hud.acceptPanel(push);
    if (new URLSearchParams(window.location.search).get('joatNoteOver') === 'news') {
      await menu.openPanelTab('news');
    }
    const now = Date.now();
    hud.acceptNotifications([
      {
        id: '1'.padStart(32, '0'), app: 'Messages', sender: 'Sam', at: now - 20_000, urgent: false,
        body: 'Didn\u2019t catch you on the phone. Can you pick up some onions and peppers on the way home?',
      },
      {
        id: '2'.padStart(32, '0'), app: 'Slack', sender: '#team-design', at: now - 4 * 60_000, urgent: false,
        body: 'A new message from Alex: \u201cThe Q3 campaign report is ready for final review in the drive.\u201d',
      },
      {
        id: '3'.padStart(32, '0'), app: 'Mail', sender: 'Billing', at: now - 47 * 60_000, urgent: false,
        body: 'Payment received. A charge of $4,500 from Fixture Studio Inc. has been processed.',
      },
    ]);
    await hud.browseNotifications();
    return;
  }

  if (isPanelVisualState(state)) {
    // The resting overview underneath, exactly as the 'overview' fixture
    // draws it, then the three pushes a connected Mac would have made.
    // `&joatHomeRest=1` leaves Main empty, so the pane shows the lit tab's
    // own card (its grid) instead of the newest notification.
    if (new URLSearchParams(window.location.search).get('joatHomeRest') !== '1') {
      await hud.showText('NEXT\nDesign review · 4:30', ContainerId.Main, 4);
      await hud.showText(
        'LAST QUERY\nWhere is the nearest coffee shop?', ContainerId.Context, 2,
      );
      await hud.showText(
        'Calendar · 8m · Design review moved to 4:30\nMail · 23m · Q3 plan is ready for review',
        ContainerId.Tray, 2,
      );
    }
    await hud.setAmbient({ temperature: '72f', condition: 'clear', unread: 2 });
    for (const push of panelVisualPushes()) await hud.acceptPanel(push);
    const tab = panelVisualTab(state);
    if (state.endsWith('-expanded') || state === 'panel-article') {
      // Through the REAL drill-in, so simulator gestures drive the page.
      await menu.openPanelTab(tab);
      if (state === 'panel-article') {
        const article = panelArticleFixture();
        await menu.presentList(
          article.title, article.items, hud.rail.requestId('news'), false, article.hint,
        );
      }
      return;
    }
    await hud.setActiveTab(tab);
    return;
  }

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
    // Base page first: transient phases render as a strip over it.
    await hud.showText('NEXT\nDesign review · 4:30', ContainerId.Main, 4);
    await hud.setAmbient({ temperature: '72f', condition: 'clear', unread: 2 });
    await hud.showProductivity(productivityLifecycleFixture(state));
    return;
  }

  if (isProductivityVisualState(state)) {
    const fixture = visualListFixture(state);
    // The Planning fixture demonstrates the split dashboard: month grid
    // left, the hovered day's items right, pane focused on the second row.
    if (state === 'calendar') {
      const grid = Array.from({ length: 42 }, (_, index) => {
        const day = new Date(Date.UTC(2026, 7, 31 + index));
        const busy = index === 1 || index === 10 || index === 17;
        return {
          day: day.toISOString().slice(0, 10),
          eventCount: busy ? 2 : 0,
          reminderCount: busy ? 1 : 0,
          inMonth: day.getUTCMonth() === 8,
          ...(busy ? {
            entries: [
              { time: '09:30', kind: 'event' as const, title: 'Design review' },
              { time: '16:30', kind: 'event' as const, title: 'Site walk with Adaeze' },
              { time: '18:00', kind: 'reminder' as const, title: 'Buy filters' },
            ],
          } : {}),
        };
      });
      // Through the REAL voice-adoption path: arms CapabilityMenu's dashboard
      // state machine so simulator-injected gestures drive hover/pane live.
      await menu.presentList(
        'PLANNING · PLAN · SEPTEMBER 2026',
        fixture.items,
        'voice-fixture-calendar',
        true,
        'tap: choose · swipe: move · 2x tap: back',
        grid,
      );
      return;
    }
    // The long thread goes through the real voice-adoption path, so the
    // simulator's swipes page it exactly as they page a Mac thread.
    if (state === 'messages-conversation-long') {
      await menu.presentList(fixture.title, fixture.items, 'voice-fixture-thread', true, fixture.hint);
      return;
    }
    // Drawn as production draws it: a reader is Even's text page.
    const nativeTextPage = !usesFirmwareList({ title: fixture.title, selectable: fixture.selectable });
    await hud.showList(fixture.items, {
      title: fixture.title,
      hint: fixture.hint,
      selectable: fixture.selectable,
      ...(nativeTextPage ? { nativeTextPage: true } : {}),
    });
    return;
  }

  // A JOAT conversation long enough to scroll: the card at its 7-line cap.
  if (state === 'assistant-conversation') {
    await hud.showAssistantConversation({
      conversationId: 'visual-conversation',
      revision: 1,
      // ?joatConvState=thinking shows the working indicator.
      state: new URLSearchParams(window.location.search).get('joatConvState') === 'thinking'
        ? 'thinking' : 'ready',
      continueAfterMs: 120_000,
      earlierTurnsOmitted: false,
      // ?joatConvTurns=short: one short exchange, the card at its first size.
      // ?joatConvTurns=question: a question JOAT is still working on.
      // ?joatConvTurns=wake: the wearer said only "JOAT"; JOAT answers.
      turns: new URLSearchParams(window.location.search).get('joatConvTurns') === 'wake' ? [
        { role: 'assistant', text: 'Yes?' },
      ] : new URLSearchParams(window.location.search).get('joatConvTurns') === 'question' ? [
        { role: 'user', text: 'What time is my next meeting?' },
      ] : new URLSearchParams(window.location.search).get('joatConvTurns') === 'short' ? [
        { role: 'user', text: 'What time is my next meeting?' },
        { role: 'assistant', text: 'Design review at 2:30.' },
      ] : [
        { role: 'user', text: 'Why is the sky blue and the sunset red?' },
        {
          role: 'assistant',
          text: "Sunlight holds every colour, and the air scatters short blue wavelengths far more than long red ones, so blue light reaches you from every part of the sky. At sunset the light crosses much more air, most of the blue is scattered away before it arrives, and the reds and oranges that remain paint the sun and the clouds near it.",
        },
      ],
    });
    return;
  }

  if (state === 'confirmation') {
    if (imu) await runVisualConfirmationFixture(hud, imu);
    else await hud.showConfirmation(VISUAL_CONFIRMATION_PROMPT, 'browser-visual-confirmation');
    return;
  }

  // A Messages reply in JOAT's conversation card: the question, then the
  // wearer's words for consent. Inert — nothing reaches the Mac.
  if (state === 'message-reply-question' || state === 'message-reply-confirm') {
    // Over the thread it was called on, as in production.
    const thread = visualListFixture('messages-conversation');
    await hud.showList(thread.items, {
      title: thread.title, hint: thread.hint, selectable: thread.selectable, nativeTextPage: true,
    });
  }
  if (state === 'message-reply-question') {
    await hud.showPromptQuestion(
      'visual-reply-question', 'What do you want to say back to Adele?', () => true, 'messages',
    );
    // `&joatReplyWords=...`: the words so far, as the Mac's prompt.partial
    // would deliver them while the wearer speaks (the simulator has no mic).
    const words = new URLSearchParams(location.search).get('joatReplyWords');
    if (words) await hud.showPromptPartial('visual-reply-question', words);
    return;
  }
  if (state === 'message-reply-confirm') {
    const id = 'visual-reply-confirm';
    const visible = await hud.showConfirmation(
      'Send to Adele? “On my way, see you at 4:30.”', id, 'messages',
    );
    if (imu) {
      imu.prepare(id, () => { void hud.dismissConfirmation(id); });
      if (visible) imu.activate(id, 8000);
      else imu.cancel(id);
    }
    return;
  }

  if (state === 'assistant-overlay') {
    await hud.showText('NEXT\nDesign review · 4:30', ContainerId.Main, 4);
    await hud.showText(
    'Calendar · 8m · Design review moved to 4:30\nMail · 23m · Q3 plan is ready for review',
    ContainerId.Tray, 2,
  );
  await hud.setAmbient({ temperature: '72f', condition: 'clear', unread: 2 });
    await hud.showAssistantOverlay(ASSISTANT_OVERLAY_VISUAL_TEXT);
    return;
  }

  await hud.showText('NEXT\nDesign review · 4:30', ContainerId.Main, 4);
  await hud.showText(
    'LAST QUERY\nWhere is the nearest coffee shop?', ContainerId.Context, 2,
  );
  await hud.showText(
    'Calendar · 8m · Design review moved to 4:30\nMail · 23m · Q3 plan is ready for review',
    ContainerId.Tray, 2,
  );
  await hud.setAmbient({ temperature: '72f', condition: 'clear', unread: 2 });
  await hud.showText('tap: open · swipe: tabs · long tap: mic', ContainerId.Hint, 1);
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
  const returnHint = '2x tap: Planning';
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
        primary: 'Due now', secondary: 'This reminder is still incomplete.',
        hint: 'tap: complete · 2x tap: Planning',
      };
  }
}

void boot().catch((err) => {
  // Keep recovery readable on a phone, without presenting disconnected
  // controls as usable or dumping native error details into the page.
  console.error('[joat] boot failed', err);
  setPanel('p-status', 'Could not start');
  setPanel('p-link', 'Even Hub initialization incomplete');
  setPanel('p-recovery', 'Close and reopen JOAT in Even Hub. Check that your glasses are connected in the Even app. If this repeats, reopen the JOAT Mac app and check its connection status.');
  document.querySelectorAll<HTMLButtonElement | HTMLSelectElement>('#panel button, #panel select')
    .forEach((control) => { control.disabled = true; });
});
