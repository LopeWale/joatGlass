import type { LinkState, MacLink } from './MacLink';
import type { Settings } from './Settings';
import type { ResumePolicy } from './types';

interface PhoneControlOptions {
  link: MacLink;
  urls: readonly string[];
  settings: Settings;
  changePolicy: (policy: ResumePolicy) => void;
  checkLocation: () => Promise<{ accuracy?: number } | null>;
  canReconnect: () => boolean;
  selectBrain: (url: string) => Promise<boolean>;
}

/** Phone-only controls. No HUD ownership, microphone stream or credentials. */
export class PhoneControlPanel {
  private switching = false;
  private readonly brain = document.querySelector<HTMLSelectElement>('#p-brain');
  private readonly policy = document.querySelector<HTMLSelectElement>('#p-resume');
  private readonly connect = document.querySelector<HTMLButtonElement>('#p-connect');

  constructor(private readonly options: PhoneControlOptions) {
    for (const url of options.urls) {
      const option = document.createElement('option');
      option.value = url;
      option.textContent = brainLabel(url);
      this.brain?.append(option);
    }
    if (this.brain) {
      this.brain.value = options.link.activeUrl;
      this.brain.disabled = false;
      this.brain.addEventListener('change', () => this.updateLink(options.link.linkState));
    }
    if (this.policy) this.policy.disabled = false;
    const locationButton = document.querySelector<HTMLButtonElement>('#p-location-check');
    if (locationButton) locationButton.disabled = false;
    this.updatePolicy(options.settings.resumePolicy);
    this.connect?.addEventListener('click', () => {
      if (!options.canReconnect() || !this.brain) return;
      void this.selectBrain(this.brain.value);
    });
    this.policy?.addEventListener('change', () => {
      const value = this.policy?.value;
      if (value === 'auto' || value === 'mac' || value === 'confirm') options.changePolicy(value);
    });
    document.querySelector<HTMLButtonElement>('#p-location-check')?.addEventListener('click', () => {
      void this.checkLocation();
    });
    this.updateLink(options.link.linkState);
  }

  updatePolicy(policy: ResumePolicy): void {
    if (this.policy) this.policy.value = policy;
  }

  updateLink(state: LinkState): void {
    const link = this.options.link;
    this.write('p-link', link.wasReplaced ? 'Another display active' : state === 'connected'
      ? 'Connected' : state === 'closed' ? 'Disconnected' : 'Connecting…');
    this.write('p-url', link.activeUrl);
    this.write('p-active-brain', brainLabel(link.activeUrl));
    this.write('p-recovery', link.wasReplaced
      ? 'Reconnect here to make this phone display active again.'
      : state === 'connected'
        ? 'The Mac connection is authenticated. AI and feature availability still depend on the services configured in that Mac’s JOAT app.'
        : 'Open JOAT on the Mac shown here and keep it awake. For a Tailscale connection, connect both devices to Tailscale. Check pairing approval in the Mac app, then reconnect.');
    if (this.connect) this.connect.disabled = this.switching || !this.options.canReconnect();
    if (this.connect) this.connect.textContent = this.switching ? 'Connecting…'
      : this.brain?.value === link.activeUrl && state === 'connected' ? 'Reconnect' : 'Connect';
    if (!this.switching && this.brain && this.brain.value !== link.activeUrl) {
      this.write('p-action', `Tap Connect to switch to ${brainLabel(this.brain.value)}. Current connection: ${brainLabel(link.activeUrl)}.`);
    } else if (state === 'connected' && !this.switching) this.write('p-action', '');
  }

  private async selectBrain(url: string): Promise<void> {
    if (this.switching || !this.options.canReconnect()) return;
    this.switching = true;
    this.updateLink(this.options.link.linkState);
    this.write('p-action', 'Switching connection…');
    try {
      const selected = await this.options.selectBrain(url);
      this.write('p-action', selected
        ? 'Connecting. Approve pairing on the Mac if prompted.'
        : 'The display could not be cleared safely. Keep Even Hub open and try Reconnect again.');
    } catch {
      this.write('p-action', 'Reconnect could not finish. Keep Even Hub open and try again.');
    } finally {
      this.switching = false;
      this.updateLink(this.options.link.linkState);
    }
  }

  private async checkLocation(): Promise<void> {
    const button = document.querySelector<HTMLButtonElement>('#p-location-check');
    if (!button || button.disabled) return;
    button.disabled = true;
    this.write('p-location-result', 'Checking the phone’s location through Even Hub…');
    try {
      const fix = await this.options.checkLocation();
      this.write('p-location-result', fix
        ? `Location is available${Number.isFinite(fix.accuracy) ? ` · reported accuracy ${Math.round(fix.accuracy!)} m` : ''}. This confirms a phone fix, not a calculated route.`
        : 'No location fix returned. Allow Location for Even Hub in phone Settings, enable precise location where available, and try again outdoors.');
    } catch {
      this.write('p-location-result', 'The location check could not complete. Check Even Hub’s location permission and try again.');
    } finally {
      button.disabled = false;
    }
  }

  private write(id: string, text: string): void {
    const node = document.getElementById(id);
    if (node) node.textContent = text;
  }
}

function brainLabel(url: string): string {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname;
    // Tailscale already named the machine; its first DNS label is what the
    // owner calls it (the same rule the Mac uses for OpenClaw hosts).
    const device = /^\d+(\.\d+){3}$/.test(host) ? host : host.split('.')[0] || 'JOAT Mac';
    const transport = host.endsWith('.ts.net') ? 'Tailscale' : 'Local network';
    return `${device} · ${transport}${parsed.protocol === 'wss:' ? ' (secure)' : ''}`;
  } catch { return 'Approved JOAT connection'; }
}
