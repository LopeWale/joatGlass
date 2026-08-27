import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import { LocationMonitor } from './LocationMonitor';
import { MacLink } from './MacLink';
import { AudioStreamer } from './AudioStreamer';
import { ImuMonitor } from './ImuMonitor';
import { EventRouter, type LifecycleHandler } from './EventRouter';
import { DeviceMonitor } from './DeviceMonitor';
import { Settings } from './Settings';
import { SettingsMenu } from './SettingsMenu';
import { ContainerId, CONFIRM_HINT, type DownlinkMessage } from './types';

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

/**
 * Pairing token storage. The Mac issues a token to the first remote client
 * (TOFU — pairing.set) and rejects unpaired LAN peers from then on. It lives
 * in localStorage, NOT in the bundle: .env rules forbid secrets in VITE_*,
 * and a token baked into an .ehpk would ship to anyone holding the file.
 */
const TOKEN_KEY = 'joat.pairing.token';

function loadToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // storage unavailable: dial bare; the Mac re-pairs if unpaired
  }
}

/** Webview status panel (index.html). Absent ids are a no-op by design. */
function setPanel(id: string, text: string): void {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

async function boot(): Promise<void> {
  const client = new BridgeClient();
  const layouts = new HudPageBuilder();
  // Bridge FIRST: the pairing token prefers HOST storage (finding 20 — the
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
  link.onState((state) => {
    setPanel('p-link', state);
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

  setPanel('p-url', link.activeUrl); // failover rotation, made visible
    if (state === 'connected') announce?.();
  });

  // Startup page (one-shot; bridge already initialized above). The second
  // argument lets a reloaded WebView adopt the host's existing page.
  await client.start(layouts.startup(), layouts.defaultLayout());

  const hud = new HudPresenter(client, layouts);
  const audio = new AudioStreamer(client, link);
  const imu = new ImuMonitor(client, link);
  const settings = new Settings();
  settings.load();
  setPanel('p-policy', settings.resumePolicy);
  settings.onChange((policy) => setPanel('p-policy', policy));
  const menu = new SettingsMenu(hud, settings, link);
  menu.micIsOpen = () => audio.isOpen;
  // Mic truth leaves the phone on every change — the Mac's pill used to
  // latch "streaming" at hello and never verify.
  audio.onState = (open) => {
    setPanel('p-mic', open ? 'streaming' : 'off');
    link.send({ kind: 'device', connect: 'connected', mic: open });
  };

  const lifecycle: LifecycleHandler = {
    onForegroundEnter: () => {
      // The host may have paused delivery while backgrounded; repaint so the
      // wearer isn't looking at a stale frame.
      void hud.setStatus(audio.isOpen ? 'JOAT · listening' : 'JOAT · mic off');
    },
    onForegroundExit: () => {
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

  new EventRouter(client, link, audio, imu, menu, lifecycle).attach();
  new DeviceMonitor(client, link, audio, imu, hud, settings).attach();

  link.onMessage(async (msg: DownlinkMessage) => {
    switch (msg.kind) {
      case 'hud.text':
        // append streams (debate turns, token-by-token replies) without a
        // full repaint; plain hud.text replaces the container.
        if (msg.append) await hud.appendText(msg.text, msg.containerId);
        else await hud.showText(msg.text, msg.containerId);
        break;
      case 'hud.list':
        await hud.showList(msg.items);
        break;
      case 'hud.rebuild':
        await hud.showDefault();
        break;
      case 'audio.control':
        if (msg.open) {
          const ok = await audio.start(
            msg.source === 'phone' ? AudioInputSource.Phone : AudioInputSource.Glasses,
          );
          setPanel('p-mic', ok ? 'streaming' : 'unavailable');
        } else {
          await audio.stop();
          setPanel('p-mic', 'off');
        }
        break;
      case 'imu.control':
        msg.open ? await imu.start() : await imu.stop();
        break;
      case 'imu.trace':
        imu.setTrace(msg.enabled);
        break;
      case 'confirm.request':
        await hud.showText(`${msg.prompt}\n\n${CONFIRM_HINT}`, ContainerId.Main);
        imu.await(msg.actionId);
        break;
      case 'pairing.set':
        // IGNORE if we already hold a token. app.json whitelists plain-ws
        // mDNS names, which any LAN peer can answer, so an unguarded handler
        // lets a rogue "Mac" overwrite our good token — locking the wearer
        // out of their real brain until localStorage is wiped by hand.
        // Re-pairing is deliberate: clear storage, or reinstall.
        if (loadToken()) break;
        // Ack only after the write SUCCEEDS: the Mac keeps the pairing
        // window open until then, so a lost frame or unavailable storage
        // costs a retry instead of stranding the phone permanently.
        try {
          localStorage.setItem(TOKEN_KEY, msg.token);
          void client.require().setLocalStorage(TOKEN_KEY, msg.token).catch(() => {});
          link.setToken(msg.token);
          link.send({ kind: 'pairing', token: msg.token });
        } catch {
          // Storage unavailable (private mode): use it for this session
          // only, and stay unpaired so the next launch can pair for real.
          link.setToken(msg.token);
        }
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

  // First light BEFORE any further host round trip: a dark lens reads as a
  // failure, and getDeviceInfo is a call into the phone that can stall.
  await hud.setStatus('JOAT · connecting');
  setPanel('p-status', 'connecting');

  // Tolerate a stalled/absent device info — both fields already default, and
  // the wearer would rather have a live HUD than an accurate serial number.
  const device = await Promise.race([
    client.require().getDeviceInfo(),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), 3000)),
  ]).catch(() => null);
  // Host account name grounds "the wearer's name" with zero configuration
  // (finding 21). Best-effort: older hosts reject the call.
  const user = await client.require().getUserInfo().catch(() => null);
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
  // The boot-time device status was fetched and thrown away (finding 18):
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
      // The fallback ear (finding 13): the phone mic, NAMED on the status
      // line — a silent source switch on a privacy surface is worse than
      // no fallback.
      micOk = await audio.start(AudioInputSource.Phone);
      label = 'JOAT · listening (phone mic)';
    }
    micStatus = micOk ? label : 'JOAT · mic unavailable';
  }
  await imu.start();
  await hud.setStatus(micStatus);

  // Wearer position -> Mac (finding 12): weather-without-asking, and the
  // brain stops answering timezone questions from the Mac's point of view.
  const location = new LocationMonitor(client, link);
  void location.start();
  setPanel('p-status', audio.isOpen ? 'listening' : 'mic off');
  setPanel('p-mic', audio.isOpen ? 'streaming' : 'off');

  async function teardown(): Promise<void> {
    await audio.stop();
    await imu.stop();
    link.close();
    client.dispose();
  }

  window.addEventListener('beforeunload', () => { void teardown(); });
}

void boot().catch((err) => {
  // Surface boot failures somewhere visible — the WebView has no console UI.
  document.body.textContent = `boot failed: ${err instanceof Error ? err.message : String(err)}`;
});
