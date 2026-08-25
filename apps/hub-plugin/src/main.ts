import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
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
const MAC_WS_URLS = (import.meta.env.VITE_MAC_WS_URL ?? 'wss://mac.local:8787/glasses')
  .split(',')
  .map((u) => u.trim())
  .filter((u) => u.length > 0);

/** Webview status panel (index.html). Absent ids are a no-op by design. */
function setPanel(id: string, text: string): void {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

async function boot(): Promise<void> {
  const client = new BridgeClient();
  const layouts = new HudPageBuilder();
  const link = new MacLink({ urls: MAC_WS_URLS });

  setPanel('p-url', link.activeUrl);
  // A reconnected socket is a fresh session on the Mac's side — without a new
  // hello the brain never learns who connected and never greets. announce() is
  // set once device info exists; the guard covers the first pre-boot 'connected'.
  let announce: (() => void) | undefined;
  link.onState((state) => {
    setPanel('p-link', state);
    setPanel('p-url', link.activeUrl); // failover rotation, made visible
    if (state === 'connected') announce?.();
  });

  // 1. Bridge, 2. startup page (one-shot) — order is mandatory. The second
  // argument lets a reloaded WebView adopt the host's existing page.
  await client.init();
  await client.start(layouts.startup(), layouts.defaultLayout());

  const hud = new HudPresenter(client, layouts);
  const audio = new AudioStreamer(client, link);
  const imu = new ImuMonitor(client, link);
  const settings = new Settings();
  settings.load();
  setPanel('p-policy', settings.resumePolicy);
  settings.onChange((policy) => setPanel('p-policy', policy));
  const menu = new SettingsMenu(hud, settings, link);

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

  const device = await client.require().getDeviceInfo();
  announce = () => {
    link.send({
      kind: 'hello',
      sn: device?.sn ?? 'unknown',
      model: device?.model ?? 'unknown',
      launchSource: client.getLaunchSource() ?? 'unknown',
    });
    // Announce the persisted policy so the Mac's session logic starts in sync.
    link.send({ kind: 'policy', resume: settings.resumePolicy });
  };
  announce();

  await hud.setStatus('JOAT · connecting');
  setPanel('p-status', 'connecting');
  // Both starts degrade to false on hosts missing the capability (the desktop
  // simulator has no IMU at all) — boot must survive either being absent.
  const micOk = await audio.start(AudioInputSource.Glasses);
  await imu.start();
  await hud.setStatus(micOk ? 'JOAT · listening' : 'JOAT · mic unavailable');
  setPanel('p-status', micOk ? 'listening' : 'mic unavailable');
  setPanel('p-mic', micOk ? 'streaming' : 'unavailable');

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
