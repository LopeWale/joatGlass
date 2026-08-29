import type { HudPresenter } from './HudPresenter';
import type { ImuMonitor } from './ImuMonitor';
import { CANVAS, ContainerId, type BrainstormHudModel } from './types';
import type {
  HudFramePainter,
  HudFrameRenderer,
  HudImageTile,
  HudVisualFrame,
} from './HudVisualSurface';

/** Copy used only by explicit development screenshot states. */
export const VISUAL_CONFIRMATION_PROMPT =
  'Send to SIMULATOR TEST (NO SEND)? “Trace only. No message will be sent.”';

const VISUAL_CONFIRMATION_ID = 'visual-confirmation';

export const PRODUCTIVITY_VISUAL_STATES = [
  'messages',
  'notifications',
  'notification-detail',
  'calendar',
  'calendar-detail',
  'tasks',
  'task-detail',
  'quicknotes',
  'quicknote-detail',
  'activity',
  'activity-detail',
  'action',
] as const;

export type ProductivityVisualState = (typeof PRODUCTIVITY_VISUAL_STATES)[number];

export const BRAINSTORM_VISUAL_STATES = [
  'brainstorm-ready',
  'brainstorm-live',
  'agent-terminal-ready',
  'agent-terminal-blocked',
] as const;

export type BrainstormVisualState = (typeof BRAINSTORM_VISUAL_STATES)[number];

export function isBrainstormVisualState(value: string | null): value is BrainstormVisualState {
  return BRAINSTORM_VISUAL_STATES.some((state) => state === value);
}

export function brainstormVisualFixture(state: BrainstormVisualState): BrainstormHudModel {
  const terminalReady = {
    screen: 'agent_terminal' as const,
    state: 'ready' as const,
    topic: 'Hermes Agent Terminal',
    collaborators: [],
    activity: 'Hermes is available as a Brainstorm collaborator.',
    endpointHost: 'home-mac.example-tailnet.ts.net',
    terminalState: 'ready' as const,
    terminalReason: 'Dedicated JOAT session',
    noToolsChecked: true,
  };
  switch (state) {
    case 'brainstorm-ready':
      return {
        screen: 'brainstorm', state: 'ready', topic: 'How should JOAT plan today?',
        collaborators: ['Codex · proposer', 'Claude · critic', 'Hermes · terminal'],
        activity: 'Say “JOAT, brainstorm …” to begin.',
        terminalState: 'ready', terminalReason: 'Dedicated JOAT session', noToolsChecked: true,
      };
    case 'brainstorm-live':
      return {
        screen: 'brainstorm', state: 'running', topic: 'How should JOAT plan today?',
        collaborators: ['Codex · proposer', 'Claude · critic', 'Hermes · terminal'],
        activeSpeaker: 'Hermes · terminal',
        activity: 'Comparing the proposal against constraints and next actions.',
        endpointHost: terminalReady.endpointHost,
        terminalState: 'running', terminalReason: 'Dedicated JOAT session', noToolsChecked: true,
      };
    case 'agent-terminal-blocked':
      return {
        ...terminalReady,
        state: 'blocked', terminalState: 'blocked', noToolsChecked: false,
        terminalReason: 'Dedicated session is not ready.',
      };
    case 'agent-terminal-ready':
      return terminalReady;
  }
}

export function isProductivityVisualState(
  value: string | null,
): value is ProductivityVisualState {
  return PRODUCTIVITY_VISUAL_STATES.some((state) => state === value);
}

export interface VisualListFixture {
  title: string;
  hint: string;
  items: string[];
  selectable: boolean;
}

export function stitchHudPreviewRgba(
  tiles: readonly HudImageTile[],
): Uint8ClampedArray {
  const rgba = new Uint8ClampedArray(CANVAS.width * CANVAS.height * 4);
  const origins: Readonly<Record<number, readonly [number, number]>> = {
    [ContainerId.Image]: [0, 0],
    [ContainerId.VisualTopRight]: [288, 0],
    [ContainerId.VisualBottomLeft]: [0, 144],
    [ContainerId.VisualBottomRight]: [288, 144],
  };
  for (const tile of tiles) {
    const origin = origins[tile.containerId];
    if (!origin || tile.pixels.length !== 288 * 144) {
      throw new Error('visual preview received an invalid HUD tile');
    }
    for (let y = 0; y < 144; y += 1) {
      for (let x = 0; x < 288; x += 1) {
        const target = ((origin[1] + y) * CANVAS.width + origin[0] + x) * 4;
        rgba[target] = 0;
        rgba[target + 1] = Math.max(0, Math.min(15, tile.pixels[y * 288 + x] ?? 0)) * 17;
        rgba[target + 2] = 0;
        rgba[target + 3] = 255;
      }
    }
  }
  return rgba;
}

/** Browser-only inspection sink. It takes the exact four Gray4 tiles emitted
 * by the production renderer and stitches them without inventing another UI. */
export class BrowserHudPreview implements HudFramePainter {
  private readonly canvas: HTMLCanvasElement;
  private readonly context: CanvasRenderingContext2D;

  constructor(
    private readonly renderer: HudFrameRenderer,
    documentRoot: Document = document,
  ) {
    this.canvas = documentRoot.createElement('canvas');
    this.canvas.width = CANVAS.width;
    this.canvas.height = CANVAS.height;
    this.canvas.setAttribute('aria-label', 'JOAT 576 by 288 HUD visual preview');
    this.canvas.style.display = 'block';
    const context = this.canvas.getContext('2d', { alpha: false });
    if (!context) throw new Error('2D canvas is unavailable');
    this.context = context;
    documentRoot.documentElement.style.background = '#000';
    documentRoot.body.style.cssText = 'margin:0;padding:0;width:576px;height:288px;background:#000;';
    documentRoot.body.replaceChildren(this.canvas);
  }

  async paint(frame: HudVisualFrame): Promise<boolean> {
    const rgba = stitchHudPreviewRgba(await this.renderer.render(frame));
    const image = this.context.createImageData(CANVAS.width, CANVAS.height);
    image.data.set(rgba);
    this.context.putImageData(image, 0, 0);
    return true;
  }
}

/** Deterministic, inert data for visual inspection through the production
 * canvas renderer. No fixture can call the Mac or mutate a native store. */
export function visualListFixture(state: ProductivityVisualState): VisualListFixture {
  const fixtures: Record<ProductivityVisualState, VisualListFixture> = {
    messages: {
      title: 'MESSAGES · 3',
      hint: 'tap select · left x2 overview',
      selectable: true,
      items: [
        '[2m] Adele · Can you send the latest mockups?',
        '[18m] Marcus · Heading to the lab in 10.',
        '[1h] Team Ops · Standup notes are ready.',
      ],
    },
    notifications: {
      title: 'NOTIFICATIONS',
      hint: 'tap detail · left x2 overview',
      selectable: true,
      items: [
        '[now] Adaeze · Messages · Call me now',
        '[8m] Calendar · Design review moved to 4:30',
        '[23m] Mail · Q3 plan is ready for review',
      ],
    },
    'notification-detail': {
      title: 'NOTIFICATIONS · DETAIL',
      hint: 'left x2 overview',
      selectable: false,
      items: ['Adaeze · Messages', 'Call me now', 'Shown just now'],
    },
    calendar: {
      title: 'CALENDAR · 7 DAYS',
      hint: 'tap detail · left x2 overview',
      selectable: true,
      items: [
        'Design review · Fri 16:30',
        'Site walk · Sat 10:00',
        'Team planning · Mon 09:15',
      ],
    },
    'calendar-detail': {
      title: 'CALENDAR · DETAIL',
      hint: 'left x2 overview',
      selectable: false,
      items: ['Design review', 'Starts Fri Aug 28 · 16:30', 'Ends Fri Aug 28 · 17:15'],
    },
    tasks: {
      title: 'TASKS',
      hint: 'tap detail · left x2 overview',
      selectable: true,
      items: [
        'Buy filters · Due today 18:00',
        'Send field notes · Due tomorrow 09:00',
        'Renew permit · Due Sep 4 12:00',
      ],
    },
    'task-detail': {
      title: 'TASKS · DETAIL',
      hint: 'left x2 overview',
      selectable: false,
      items: ['Buy filters', 'Due today · 18:00', 'Complete by voice · exact title'],
    },
    quicknotes: {
      title: 'QUICKNOTES',
      hint: 'tap detail · left x2 overview',
      selectable: true,
      items: [
        '[4m] Ask Adaeze about the design review',
        '[2h] Verify the west entrance sensor',
        '[1d] Idea: quiet morning briefing mode',
      ],
    },
    'quicknote-detail': {
      title: 'QUICKNOTES · DETAIL',
      hint: 'left x2 overview',
      selectable: false,
      items: ['Ask Adaeze about the design review'],
    },
    activity: {
      title: 'ACTIVITY',
      hint: 'tap detail · left x2 overview',
      selectable: true,
      items: [
        'Calendar create · Succeeded',
        'Reminders create · Uncertain',
        'Message reply · Rejected',
      ],
    },
    'activity-detail': {
      title: 'ACTIVITY · DETAIL',
      hint: 'left x2 overview',
      selectable: false,
      items: ['Calendar create', 'State · Succeeded', 'Action ref · 4b31a870'],
    },
    action: {
      title: 'ACTION · DONE',
      hint: 'left x2 overview',
      selectable: false,
      items: ['Calendar create · Saved and verified'],
    },
  };
  return fixtures[state];
}

/**
 * Arm the explicit development confirmation fixture through the production
 * gate. The fixture is inert with respect to the Mac and external senders,
 * but its simulator Click/Up/Down gestures must behave like the real surface.
 */
export async function runVisualConfirmationFixture(
  hud: Pick<HudPresenter, 'showConfirmation' | 'dismissConfirmation'>,
  imu: Pick<ImuMonitor, 'prepare' | 'activate' | 'cancel'>,
): Promise<boolean> {
  const rendered = hud.showConfirmation(VISUAL_CONFIRMATION_PROMPT, VISUAL_CONFIRMATION_ID);
  imu.prepare(VISUAL_CONFIRMATION_ID, () => {
    void hud.dismissConfirmation(VISUAL_CONFIRMATION_ID);
  });

  let visible = false;
  try {
    visible = await rendered;
  } catch (error) {
    console.error('[joat] visual confirmation render failed:', error);
  }
  if (visible) imu.activate(VISUAL_CONFIRMATION_ID, 8000);
  else imu.cancel(VISUAL_CONFIRMATION_ID);
  return visible;
}
