import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import { MacLink } from './MacLink';
import { AudioStreamer } from './AudioStreamer';
import { ImuMonitor } from './ImuMonitor';
import { EventRouter } from './EventRouter';
import { DeviceMonitor } from './DeviceMonitor';
import { Settings } from './Settings';
import { SettingsMenu } from './SettingsMenu';
import { ContainerId, type DownlinkMessage } from './types';

/** Must be an exact-match domain in app.json (no wildcards) or the Hub blocks it. */
const MAC_WS_URL = import.meta.env.VITE_MAC_WS_URL ?? 'wss://mac.local:8787/glasses';

async function boot(): Promise<void> {
  const client = new BridgeClient();
  const layouts = new HudPageBuilder();
  const link = new MacLink({ url: MAC_WS_URL });

  // 1. Bridge, 2. startup page (one-shot) — order is mandatory.
  await client.init();
  await client.start(layouts.startup());

  const hud = new HudPresenter(client, layouts);
  const audio = new AudioStreamer(client, link);
  const imu = new ImuMonitor(client, link);
  const settings = new Settings();
  settings.load();
  const menu = new SettingsMenu(hud, settings, link);
  new EventRouter(client, link, audio, imu, menu).attach();
  new DeviceMonitor(client, link, audio, imu, hud, settings).attach();

  link.onMessage(async (msg: DownlinkMessage) => {
    switch (msg.kind) {
      case 'hud.text':
        await hud.showText(msg.text, msg.containerId);
        break;
      case 'hud.list':
        await hud.showList(msg.items);
        break;
      case 'hud.rebuild':
        await hud.showDefault();
        break;
      case 'audio.control':
        msg.open
          ? await audio.start(msg.source === 'phone' ? AudioInputSource.Phone : AudioInputSource.Glasses)
          : await audio.stop();
        break;
      case 'imu.control':
        msg.open ? await imu.start() : await imu.stop();
        break;
      case 'confirm.request':
        await hud.showText(`${msg.prompt}\n\nNod or tap ring to confirm.`, ContainerId.Main);
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
  link.send({
    kind: 'hello',
    sn: device?.sn ?? 'unknown',
    model: device?.model ?? 'unknown',
    launchSource: client.getLaunchSource() ?? 'unknown',
  });
  // Announce the persisted policy so the Mac's session logic starts in sync.
  link.send({ kind: 'policy', resume: settings.resumePolicy });

  await hud.setStatus('JOAT · connecting');
  await audio.start(AudioInputSource.Glasses);
  await imu.start();
  await hud.setStatus('JOAT · listening');

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
