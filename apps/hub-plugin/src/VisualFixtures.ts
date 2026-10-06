import type { HudPresenter } from './HudPresenter';
import type { ImuMonitor } from './ImuMonitor';
import {
  CANVAS,
  ContainerId,
  type BrainstormHudModel,
  type PanelContentTab,
} from './types';
import type { HudPanelMessage } from './PanelRail';
import type {
  HudFramePainter,
  HudFrameRenderer,
  HudImageTile,
  HudVisualFrame,
} from './HudVisualSurface';

/** Copy used only by explicit development screenshot states. */
export const VISUAL_CONFIRMATION_PROMPT =
  'Send to SIMULATOR TEST (NO SEND)? “Trace only. No message will be sent.”';

/** Content-free development copy that exercises the longest assistant card
 * through the production four-tile renderer without exposing a real reply. */
export const ASSISTANT_OVERLAY_VISUAL_TEXT =
  'I checked the calendar and reminders. Your design review is next, and reminder monitoring is armed for this evening.';

const VISUAL_CONFIRMATION_ID = 'visual-confirmation';

export const PRODUCTIVITY_VISUAL_STATES = [
  'messages',
  'messages-conversation',
  'messages-conversation-long',
  'messages-compose',
  'notifications',
  'notification-detail',
  'calendar',
  'calendar-detail',
  'tasks',
  'task-detail',
  'quicknotes',
  'quicknote-detail',
  'quicknote-recording',
  'quicknote-discard',
  'quicknote-saving',
  'activity',
  'activity-detail',
  'action',
  // The two pickers. Both surfaces used to tell the wearer to SAY a phrase
  // to see what already existed; these are what they show instead, and
  // they need to be visually checkable like every other list.
  'brainstorm-sessions',
  'terminal-sessions',
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
    // The LANE, in the roster's "<lane> · <state>" shape — which is what
    // AgentTerminalRoster.frame() actually sends. This fixture said
    // "Hermes Agent Terminal", so the header rendered
    // "HERMES AGENT TERMINAL > READY · NO TOOLS CHECKED BEFOR…" and I was
    // reading a truncation that production never produces.
    topic: 'work · ready',
    // Lane chips: every concurrent session, so one glance covers the
    // roster. Empty here meant the strip could not be seen at all.
    collaborators: ['work·idle', 'calendar·run', 'gateway·idle'],
    // Not an instruction to recite a phrase. The wearer asked what "say
    // terminal sessions to choose a work session" even meant — opening the
    // terminal shows the sessions now, and this is the empty case.
    activity: 'No work sessions yet. Say “terminal <instruction>” to start one.',
    endpointHost: 'home-mac.example-tailnet.ts.net',
    terminalReason: 'Dedicated JOAT session',
    noToolsChecked: true,
  };
  switch (state) {
    case 'brainstorm-ready':
      return {
        screen: 'brainstorm', state: 'ready', topic: 'How should JOAT plan today?',
        collaborators: ['Codex · proposer', 'Claude · critic'],
        activity: 'Say “JOAT, brainstorm …” to begin.',
      };
    case 'brainstorm-live':
      return {
        screen: 'brainstorm', state: 'running', topic: 'How should JOAT plan today?',
        collaborators: ['Codex · proposer', 'Claude · critic'],
        activeSpeaker: 'Claude · critic',
        // Only the SEATED collaborators speak here. This fixture used to
        // put HERMES in the Brainstorm transcript, which is the one thing
        // the two surfaces are defined against each other by: Codex and
        // Claude are Brainstorm collaborators, Hermes and OpenClaw are
        // exclusively Agent Terminal workers. A fixture is what the HUD
        // gets verified against, so a wrong one verifies the wrong screen.
        //
        // These are WHOLE utterances with no hand-inserted breaks. The
        // previous version wrapped them by hand into short lines, which
        // made the lens look two-thirds empty and — worse — meant the
        // wrapper under test was never once exercised by the fixture used
        // to check it. Let the renderer wrap; that is the thing being
        // verified. It ends on the marker the renderer animates.
        activity: [
          'JOAT > Round 1 — independent reviews. Codex first.',
          'CODEX > Cache the last synthesis and replay it on reconnect, so the wearer never has to watch a round they already read a second time.',
          'CLAUDE > That assumes the reconnect resumes the same session. After a lens handoff it does not: the seven-packet auth replay resets the page and the id with it.',
          'JOAT > Unsupported claim: "never watch a round twice". Returning to Codex for repair.',
          'CODEX > Withdrawn. Cache keyed on session id and replayed only while that id survives; a handoff starts a new one and redraws from the top.',
          'JOAT > Verified. Round 2 — cross-review.',
          'CLAUDE > Then the cache buys nothing on the one path that needed it. Ship the redraw and drop the cache entirely.',
          'CODEX > Agreed for reconnect. It still pays on foreground return, which is the common case and keeps the same id.',
          'JOAT > Round 3 — meta-review.',
          'CLAUDE > Thinking…',
        ].join('\n'),
        // No endpointHost, terminalReason, noToolsChecked — or
        // terminalState, which is gone from the render model entirely
        // (it always mirrored `state` on real frames): the wire stopped
        // carrying them on a Brainstorm frame when the two features were
        // split (docs/SESSION_MODEL.md:74-75), so a fixture that sets
        // them is drawing a screen the engine cannot produce. `screen`
        // stays: the RENDER model uses it to pick the surface.
      };
    case 'agent-terminal-blocked':
      return {
        ...terminalReady,
        state: 'blocked', noToolsChecked: false,
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
        rgba[target + 1] = tile.pixels[y * 288 + x] ?? 0;
        rgba[target + 2] = 0;
        rgba[target + 3] = 255;
      }
    }
  }
  return rgba;
}

/** Browser-only inspection sink. It takes the exact four Gray8 tiles emitted
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

  /** The preview has no glass to hand a page to: a firmware page simply
   * replaces the pixels with nothing, as the dev host accepts every rebuild. */
  async withPageTransition(replacePage: () => Promise<boolean>): Promise<boolean> {
    return replacePage();
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
    // The thread picker speaks the dense focus-list language: dim age chip,
    // ● only on unread threads, sender bright-ish, preview as quiet detail.
    'brainstorm-sessions': {
      title: 'BRAINSTORM',
      hint: 'tap: open · say “JOAT, brainstorm …” to start',
      selectable: true,
      items: [
        '+ New brainstorm',
        'cold-start latency · 11 turns · 7h ago',
        'BLE handoff retries · 13 turns · 8h ago',
        'naming the mic gate · 12 turns · 8h ago',
        'whether the verifier should block · 14 turns · 1d ago',
        'tabs or spaces · 6 turns · 2d ago',
        'wake-word false positives · 9 turns · 3d ago',
        'why the handoff drops a frame · 11 turns · 4d ago',
      ],
    },
    'terminal-sessions': {
      title: 'AGENT TERMINAL',
      hint: 'tap: open · say “terminal …” to send',
      selectable: true,
      items: [
        '▸ work · working · run the http audit',
        '  calendar · needs recovery · 1 waiting',
        '  gateway · idle',
      ],
    },
    messages: {
      title: 'MESSAGES',
      hint: 'tap: select · left x2: back',
      selectable: true,
      items: [
        'New message · voice recipient + text',
        '[2m] ● Adele · Can you send the latest mockups?',
        '[18m] Marcus · Heading to the lab in 10.',
        '[1h] ● Team Ops (group) · Standup notes are ready.',
        '[3h] Dele · Attachment',
      ],
    },
    // The thread as one reader, newest first; a tap replies.
    // Mirrors capabilities._show_message_history_page.
    'messages-conversation': {
      title: 'MESSAGES · CONVERSATION',
      hint: 'down more · tap: reply · left x2: back',
      selectable: true,
      items: [
        '1/1+ · Adele',
        'Adele: You are a lifesaver. See you at 4:30.',
        'You: Both palettes are in the file.',
        'Adele: Perfect. Dele wants the dark variants too.',
        'You: Exporting them now, give me ten minutes.',
        'Adele: Can you send the latest mockups before the review?',
      ],
    },
    // A full thread page as the Mac sends it: paginate_reader_text's
    // lossless 62-column slices, a link cut mid-token included.
    'messages-conversation-long': {
      title: 'MESSAGES · CONVERSATION',
      hint: 'down more · tap: reply · left x2: back',
      selectable: true,
      items: [
        '1/2 · Adele',
        'Adele: Can you send the latest mockups before the review? I ',
        'want to walk Marcus through the onboarding flow first.\n',
        'You: Exporting them now, give me ten minutes. The dark ',
        'variants are still rendering.\n',
        'Adele: Perfect. Dele wants the dark variants too, and the ',
        'empty states for the inbox screen.\n',
        'You: Both palettes are in the file. Link: ',
        'https://www.figma.com/design/k3J9xQ2/joat-hud-review?node-id=1',
        '204-5521&t=Ab12\n',
        'Adele: You are a lifesaver. See you at 4:30 in the big room, ',
        "I'll bring coffee.\n",
        'You: Thanks! Running two minutes late, start without me if ',
        'Marcus is already there.\n',
        "Adele: No rush. We're still waiting on the projector anyway.\n",
        'Adele: Also, can you remind me which build has the new ',
        'header? The one with the battery icon on the right.\n',
        'You: 0.1.107 — the image header is on every page now.',
      ],
    },
    'messages-compose': {
      title: 'MESSAGES · COMPOSE',
      hint: 'speak: recipient · left x2: back',
      selectable: false,
      items: [
        'Say the recipient name first.',
        'Then say the message.',
        'One matching Mac contact is required.',
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
    // One unified surface: recordings (transcribed + summarized) first,
    // exact dictated text notes after — mirroring capabilities._show_notes.
    quicknotes: {
      title: 'QUICKNOTES',
      hint: 'tap: detail · left x2: back',
      selectable: true,
      items: [
        '[4m] ● Client standup',
        '[2h] ● Field walkthrough · gaps',
        '[2h] Ask Adaeze about the design review',
        '[1d] Idea: quiet morning briefing mode',
      ],
    },
    // A recording's summary page (capabilities._show_voice_note_detail).
    'quicknote-detail': {
      title: 'QUICKNOTES · NOTE',
      hint: 'say: ask the last note … · left x2: back',
      selectable: false,
      items: [
        'Client standup',
        '2 transcript sections',
        'SUMMARY',
        'The team agreed to ship the beta on Friday. QA owns',
        'the final pass; Dele drafts the release notes tonight.',
        'KEY POINTS',
        '– Ship the beta on Friday',
        '– QA signs off Thursday',
        'TODOS',
        '□ Email release notes to the team',
        '□ Book the retro room',
      ],
    },
    // Live capture owns the page; the exclusive microphone belongs to the
    // recording, so these tappable rows are the wearer's only controls.
    'quicknote-recording': {
      title: 'QUICKNOTES · RECORDING',
      hint: 'Client standup · tap: choose',
      selectable: true,
      items: ['Save and finish', 'Pause recording', 'Discard recording'],
    },
    'quicknote-discard': {
      title: 'QUICKNOTES · DISCARD?',
      hint: 'Client standup · tap: choose',
      selectable: true,
      items: ['Confirm discard', 'Keep recording'],
    },
    'quicknote-saving': {
      title: 'QUICKNOTES · SAVING',
      hint: 'left x2 overview',
      selectable: false,
      items: ['Finishing the recording…', 'Transcribing on this Mac.'],
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

// ── The overview's tab rail ──────────────────────────────────────────────

/** Development-only lenses for the panel tabs: each resting tab, each
 * expanded tab, and an opened article. Every string here is shaped by the
 * REAL producer — `Headline.row`, `SearchResult.row`, `HudPanelPomodoro`
 * and `PanelPublisher._send_reader` in apps/mac/joat/services — so the
 * simulator proves a screen the Mac actually sends, not an invented one. */
export const PANEL_VISUAL_STATES = [
  'panel-news',
  'panel-pomodoro',
  'panel-search',
  'panel-news-expanded',
  'panel-pomodoro-expanded',
  'panel-search-expanded',
  'panel-article',
] as const;

export type PanelVisualState = (typeof PANEL_VISUAL_STATES)[number];

export function isPanelVisualState(value: string | null): value is PanelVisualState {
  return PANEL_VISUAL_STATES.some((state) => state === value);
}

export function panelVisualTab(state: PanelVisualState): PanelContentTab {
  if (state.startsWith('panel-pomodoro')) return 'pomodoro';
  if (state.startsWith('panel-search')) return 'search';
  return 'news';
}

/** The three pushes a connected Mac would have made, content-free. News
 * rows follow "[age] title · source" within HudList's 64-character cap;
 * twelve of them, more than the firmware list shows at once. */
export function panelVisualPushes(nowMs = Date.now()): HudPanelMessage[] {
  const focusLeftMs = 14 * 60_000 + 37_000;
  return [
    {
      kind: 'hud.panel',
      tab: 'news',
      revision: 1,
      rows: [
        '[12m] Consumer sentiment rises to 48.9 after four months… · BBC',
        '[25m] Apple announces price rises on MacBooks and iPads · NPR',
        '[1h] NASA telescope finds clues to ancient comets · Reuters',
        '[2h] Rates held as inflation cools for a third month · BBC',
        '[3h] Rover lands in Jezero crater after seven months · NPR',
        '[4h] Council approves the riverfront transit plan · Guardian',
        '[5h] Chip makers report record quarter on AI demand · Reuters',
        '[6h] Storm season opens with two named systems at sea · NPR',
        '[7h] Startup unveils a foldable e-ink reader for outdoors · BBC',
        '[9h] Museum reopens with a restored Renaissance wing · Guardian',
        '[11h] Local library extends weekend hours through winter · NPR',
        '[13h] Electric ferry begins service across the harbour · BBC',
      ],
    },
    {
      kind: 'hud.panel',
      tab: 'pomodoro',
      revision: 1,
      rows: ['Pause', 'Skip to break', 'Reset'],
      pomodoro: {
        phase: 'focus',
        running: true,
        remainingMs: focusLeftMs,
        lengthMs: 1_500_000,
        completed: 1,
        endsAtUnixMs: nowMs + focusLeftMs,
      },
    },
    {
      kind: 'hud.panel',
      tab: 'search',
      revision: 1,
      headline: 'Even Realities G2 review',
      rows: [
        'Even Realities G2 review: subtle, useful, pricey · theverge.com',
        'G2 smart glasses — Even Realities · evenrealities.com',
        'What do you think of the G2? · reddit.com',
        'Even Realities G2 vs Meta Ray-Ban Display · tomsguide.com',
      ],
    },
  ];
}

/** An opened headline, as PanelPublisher sends it: a metadata line with
 * the page position, then the page as word-bounded chunks the plugin
 * re-wraps, under a hint that names only the pages that exist. */
export function panelArticleFixture(): { title: string; items: string[]; hint: string } {
  return {
    title: 'NEWS · ARTICLE',
    items: [
      'BBC · 12m · Consumer sentiment rises to 48.9 · 1/3',
      'Consumer sentiment in the US rose slightly this month,',
      'reaching 48.9 after four consecutive months of decline,',
      'according to a survey published on Tuesday. The reading',
      'remains well below its long-run average, and respondents',
      'continued to cite prices and borrowing costs as their main',
      'concerns. Economists said the small improvement was',
      'consistent with a labour market that is cooling without',
      'breaking, and with fuel prices that have eased.',
    ],
    hint: 'down more · left x2: back',
  };
}
