import type { LinkState, MacLink } from './MacLink';
import type { MicMode, ResumePolicy } from './types';

export interface PhoneControlHandoffView {
  readonly active: boolean;
  readonly capability?: string;
  readonly homeUrl?: string;
  readonly lastMacUrl?: string | null;
}

export interface PhoneControlOptions {
  link: Pick<MacLink, 'activeUrl' | 'linkState' | 'wasReplaced'>;
  urls: readonly string[];
  settings: { readonly resumePolicy: ResumePolicy; readonly micMode?: MicMode };
  handoff?: PhoneControlHandoffView;
  changePolicy: (policy: ResumePolicy) => void;
  changeMicMode?: (mode: MicMode) => void;
  checkLocation: () => Promise<{ accuracy?: number } | null>;
  canReconnect: () => boolean;
  selectBrain: (url: string) => Promise<boolean>;
}

/** Phone-only controls. No HUD ownership, microphone stream or credentials. */
export class PhoneControlPanel {
  private switching = false;
  private userEditedBrain = false;
  private readonly brain = document.querySelector<HTMLSelectElement>('#p-brain');
  private readonly policy = document.querySelector<HTMLSelectElement>('#p-resume');
  private readonly micMode = document.querySelector<HTMLSelectElement>('#p-mic-mode');
  private readonly connect = document.querySelector<HTMLButtonElement>('#p-connect');

  constructor(private readonly options: PhoneControlOptions) {
    for (const url of options.urls) {
      const option = document.createElement('option');
      option.value = url;
      option.textContent = brainLabel(url);
      this.brain?.append(option);
    }
    if (this.brain) {
      this.brain.value = this.primaryUrl();
      this.brain.disabled = false;
      this.brain.addEventListener('change', () => {
        this.userEditedBrain = this.brain !== null && this.brain.value !== this.primaryUrl();
        this.updateLink(options.link.linkState);
      });
    }
    if (this.policy) this.policy.disabled = false;
    if (this.micMode) this.micMode.disabled = false;
    const locationButton = document.querySelector<HTMLButtonElement>('#p-location-check');
    if (locationButton) locationButton.disabled = false;
    this.updatePolicy(options.settings.resumePolicy);
    if (options.settings.micMode) this.updateMicMode(options.settings.micMode);
    this.connect?.addEventListener('click', () => {
      if (!options.canReconnect() || !this.brain) return;
      void this.selectBrain(this.brain.value);
    });
    this.policy?.addEventListener('change', () => {
      const value = this.policy?.value;
      if (value === 'auto' || value === 'mac' || value === 'confirm') options.changePolicy(value);
    });
    this.micMode?.addEventListener('change', () => {
      const value = this.micMode?.value;
      if (value === 'glasses' || value === 'phone' || value === 'on_demand') {
        options.changeMicMode?.(value);
      }
    });
    document.querySelector<HTMLButtonElement>('#p-location-check')?.addEventListener('click', () => {
      void this.checkLocation();
    });
    this.updateLink(options.link.linkState);
  }

  updatePolicy(policy: ResumePolicy): void {
    if (this.policy) this.policy.value = policy;
  }

  updateMicMode(mode: MicMode): void {
    if (this.micMode) this.micMode.value = mode;
  }

  updateLink(state: LinkState): void {
    const link = this.options.link;
    const handoff = this.options.handoff;
    const handoffActive = handoff?.active === true;
    const primaryUrl = this.primaryUrl();

    const pinnedRow = document.getElementById('p-mac-pinned-row');
    if (pinnedRow) pinnedRow.hidden = !handoffActive;
    this.write('p-mac-pinned', handoffActive ? brainLabel(link.activeUrl) : '');
    this.write('brain-help', isPhoneBrain(primaryUrl)
      ? 'Connected to JOAT Brain on this iPhone. Connect switches only when you choose another brain.'
      : 'Connect retries or switches to the brain you choose.');

    if (this.brain && !this.userEditedBrain && this.brain.value !== primaryUrl) {
      this.brain.value = primaryUrl;
    }

    this.write('p-link', link.wasReplaced ? 'Another display active' : state === 'connected'
      ? 'Connected' : state === 'closed' ? 'Disconnected' : 'Connecting…');
    this.write('p-url', link.activeUrl);
    this.write('p-active-brain', brainLabel(link.activeUrl));

    const handoffSummary = handoffActive
      ? this.describeHandoff(state, link.activeUrl, handoff?.capability, handoff?.homeUrl)
      : undefined;

    this.write('p-recovery', link.wasReplaced
      ? 'Reconnect here to make this phone display active again.'
      : handoffSummary !== undefined
        ? handoffSummary
        : state === 'connected'
          ? isPhoneBrain(link.activeUrl)
            ? 'JOAT Brain is active on this iPhone. All glasses pages stay connected locally on this iPhone.'
            : 'The Mac connection is authenticated. AI and feature availability still depend on the services configured in that Mac’s JOAT app.'
          : isPhoneBrain(link.activeUrl)
            ? 'Open JOAT Brain on this iPhone, then reconnect.'
            : 'Open JOAT on the Mac shown here and keep it awake. For a Tailscale connection, connect both devices to Tailscale. Check pairing approval in the Mac app, then reconnect.');

    if (this.connect) this.connect.disabled = this.switching || !this.options.canReconnect();
    if (this.connect) {
      this.connect.textContent = this.switching
        ? 'Connecting…'
        : this.brain?.value === primaryUrl && state === 'connected' ? 'Reconnect' : 'Connect';
    }

    if (!this.switching) {
      if (this.brain && this.brain.value !== primaryUrl) {
        this.write('p-action', `Tap Connect to switch to ${brainLabel(this.brain.value)}. Current connection: ${brainLabel(link.activeUrl)}.`);
      } else if (handoffSummary !== undefined) {
        this.write('p-action', handoffSummary);
      } else if (state === 'connected') {
        this.write('p-action', '');
      }
    }
  }

  private primaryUrl(): string {
    const handoff = this.options.handoff;
    if (handoff?.active && handoff.homeUrl) return handoff.homeUrl;
    return this.options.link.activeUrl;
  }

  private describeHandoff(
    state: LinkState,
    macUrl: string,
    capability: string | undefined,
    homeUrl: string | undefined,
  ): string {
    const feature = capability ?? 'Mac feature';
    const macLabel = brainLabel(macUrl);
    const homeLabel = homeUrl ? brainLabel(homeUrl) : 'This iPhone · JOAT Brain';
    return state === 'connected'
      ? `${feature} is active on ${macLabel}. 2× tap back to Home returns to ${homeLabel}.`
      : `Opening ${feature} on ${macLabel}… 2× tap back to Home returns to ${homeLabel}.`;
  }

  private async selectBrain(url: string): Promise<void> {
    if (this.switching || !this.options.canReconnect()) return;
    this.switching = true;
    this.updateLink(this.options.link.linkState);
    this.write('p-action', 'Switching connection…');
    try {
      const selected = await this.options.selectBrain(url);
      this.userEditedBrain = false;
      this.write('p-action', selected
        ? isPhoneBrain(url)
          ? 'Connecting to JOAT Brain on this iPhone…'
          : 'Connecting. Approve pairing on the Mac if prompted.'
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

/** JOAT Brain on the phone itself (apps/ios-companion), the lab packages'
 * loopback candidate. */
export function isPhoneBrain(url: string): boolean {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'ws:' && (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost');
  } catch { return false; }
}

export function brainLabel(url: string): string {
  if (isPhoneBrain(url)) return 'This iPhone · JOAT Brain';
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
