import type { HudPresenter } from './HudPresenter';
import type { ImuMonitor } from './ImuMonitor';
import {
  CANVAS,
  ContainerId,
  type AgentTerminalHudModel,
  type PanelContentTab,
} from './types';
import type { HudPanelMessage } from './PanelRail';
import { readerRows } from './EvenFont';
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
  'reminders',
  'calendar-detail',
  'tasks',
  'task-detail',
  'quicknotes',
  'quicknote-detail',
  'quicknote-recording',
  // An open note's AI cues & prep, a cue, the prep notes (the phone brain, 2026-10-30).
  'quicknote-cues',
  'quicknote-cue',
  'quicknote-prep',
  'quicknote-discard',
  'quicknote-saving',
  'activity',
  'activity-detail',
  'action',
  'terminal-sessions',
] as const;

export type ProductivityVisualState = (typeof PRODUCTIVITY_VISUAL_STATES)[number];

export const TERMINAL_VISUAL_STATES = [
  'agent-terminal-ready',
  'agent-terminal-working',
  'agent-terminal-approval',
  'agent-terminal-blocked',
] as const;

export type TerminalVisualState = (typeof TERMINAL_VISUAL_STATES)[number];

export function isTerminalVisualState(value: string | null): value is TerminalVisualState {
  return TERMINAL_VISUAL_STATES.some((state) => state === value);
}

export function terminalVisualFixture(state: TerminalVisualState): AgentTerminalHudModel {
  const terminalReady: AgentTerminalHudModel = {
    state: 'ready',
    focus: 'Claude · idle',
    lanes: ['Claude · idle', 'Codex · working', 'Antigravity · idle'],
    activity: 'Ready',
    terminalReason: 'Dedicated JOAT session',
    conversationId: 'fixture-claude-session',
    messages: [],
  };
  switch (state) {
    case 'agent-terminal-working':
      return {
        ...terminalReady,
        state: 'running',
        focus: 'Claude · working',
        messages: [
          { id: 'u1', role: 'user', text: 'Read pyproject.toml and summarize the runtime dependencies.' },
          {
            id: 'a1',
            role: 'assistant',
            text: [
              '⏺ read_file: pyproject.toml',
              'The Mac engine runs on Python 3.12 with asyncio, google-antigravity, and aiohttp for the local endpoints.',
            ].join('\n'),
          },
        ],
      };
    case 'agent-terminal-approval':
      return {
        ...terminalReady,
        state: 'running',
        focus: 'Claude · approval needed',
        approvals: 1,
        messages: [
          { id: 'u1', role: 'user', text: 'Check git status in the repository.' },
          { id: 's1', role: 'status', text: '[approval] run_command: git status --short' },
        ],
      };
    case 'agent-terminal-blocked':
      return {
        ...terminalReady,
        state: 'blocked',
        focus: 'Claude · blocked',
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
    'terminal-sessions': {
      title: 'AGENT TERMINAL',
      hint: 'tap: sessions · 2x tap: home',
      selectable: true,
      items: [
        'Claude · idle',
        'Codex · working',
        'Antigravity · approval needed',
      ],
    },
    messages: {
      title: 'MESSAGES',
      hint: 'tap: select · 2x tap: back',
      selectable: true,
      items: [
        'New message · voice recipient + text',
        '[2m] ● Casey · Can you send the latest mockups?',
        '[18m] Morgan · Heading to the lab in 10.',
        '[1h] ● Team Ops (group) · Standup notes are ready.',
        '[3h] Evan · Attachment',
      ],
    },
    // The thread as one reader, newest first; a tap replies. Sent whole in
    // the reader template (who, a blank line, a message a line) for the
    // glasses to scroll. Mirrors capabilities._show_message_history_page.
    'messages-conversation': {
      title: 'MESSAGES · CONVERSATION',
      hint: 'tap: reply · 2x tap: back',
      selectable: true,
      items: readerRows([], [
        'Casey', '',
        'Casey: You are a lifesaver. See you at 4:30.',
        'You: Both palettes are in the file.',
        'Casey: Perfect. Evan wants the dark variants too.',
        'You: Exporting them now, give me ten minutes.',
        'Casey: Can you send the latest mockups before the review?',
      ].join('\n')),
    },
    // A thread long enough for several stretches of the scroll box, a long
    // link included.
    'messages-conversation-long': {
      title: 'MESSAGES · CONVERSATION',
      hint: 'tap: reply · 2x tap: back',
      selectable: true,
      items: readerRows([], [
        'Casey', '',
        'You: 0.1.107 — the image header is on every page now.',
        'Casey: Also, can you remind me which build has the new header? The one with the battery icon on the right.',
        "Casey: No rush. We're still waiting on the projector anyway.",
        'You: Thanks! Running two minutes late, start without me if Morgan is already there.',
        "Casey: You are a lifesaver. See you at 4:30 in the big room, I'll bring coffee.",
        'You: Both palettes are in the file. Link: https://www.figma.com/design/k3J9xQ2/joat-hud-review?node-id=1204-5521&t=Ab12',
        'Casey: Perfect. Evan wants the dark variants too, and the empty states for the inbox screen.',
        'You: Exporting them now, give me ten minutes. The dark variants are still rendering.',
        'Casey: Can you send the latest mockups before the review? I want to walk Morgan through the onboarding flow first.',
        'You: Morning! The review moved to 4:30, so there is time for one more pass on the inbox.',
        'Casey: Great. Did legal sign off on the new consent copy, or is that still waiting on Thursday?',
        'You: Still waiting. I will chase it after lunch and send you whatever they say.',
        'Casey: Thanks. And can you bring the printed flows? Morgan prefers paper for the first walk-through.',
        'You: Will do, two copies of each flow, the onboarding one in colour.',
        'Casey: Last thing: the empty state illustrations, are those final or placeholders?',
        'You: Placeholders. The final set lands Monday from the illustrator.',
        'Casey: Perfect, that works for the review. See you later.',
        'You: See you at 4:30.',
      ].join('\n')),
    },
    'messages-compose': {
      title: 'MESSAGES · COMPOSE',
      hint: 'speak: recipient · 2x tap: back',
      selectable: false,
      items: [
        'Say the recipient name first.',
        'Then say the message.',
        'One matching Mac contact is required.',
      ],
    },
    notifications: {
      title: 'NOTIFICATIONS',
      hint: 'tap: read · left x2: back',
      selectable: true,
      items: [
        '[now] Taylor · Messages · Call me now',
        '[8m] Calendar · Design review moved to 4:30',
        '[23m] Mail · Q3 plan is ready for review',
      ],
    },
    'notification-detail': {
      title: 'NOTIFICATIONS · DETAIL',
      hint: 'left x2: back',
      selectable: false,
      items: ['Taylor · Messages', 'Call me now', 'Shown just now'],
    },
    // JOAT Brain's Reminders page (Reminders.listPage): overdue first, the
    // overview tab's labels; main.ts adds the next-up card.
    reminders: {
      title: 'REMINDERS · 8 OPEN',
      hint: 'tap: done · 2x tap: back',
      selectable: true,
      items: [
        '[Late] Pay the electric bill',
        '[3:00 PM] Pick up the cake from the bakery',
        '[6:30 PM] Call Mom about the flights',
        '[Today] Buy filters for the shop',
        '[Tmrw] 9:00 AM Gym',
        '[Wed] 5:00 PM Drop permit forms at the city office',
        '[Fri] Renew the car tag',
        '[No date] Sort the garage',
      ],
    },
    'calendar-detail': {
      title: 'CALENDAR · DETAIL',
      hint: 'left x2 Reminders',
      selectable: false,
      items: ['Design review', 'Starts Fri Aug 28 · 16:30', 'Ends Fri Aug 28 · 17:15'],
    },
    tasks: {
      title: 'TASKS',
      hint: 'tap: detail · left x2: back',
      selectable: true,
      items: [
        'Buy filters · Due today 18:00',
        'Send field notes · Due tomorrow 09:00',
        'Renew permit · Due Sep 4 12:00',
      ],
    },
    'task-detail': {
      title: 'TASKS · DETAIL',
      hint: 'left x2 Reminders',
      selectable: false,
      items: ['Buy filters', 'Due today · 18:00', 'Complete by voice · exact title'],
    },
    // One unified surface: recordings (transcribed + summarized) first,
    // exact dictated text notes after — mirroring capabilities._show_notes.
    quicknotes: {
      title: 'QUICKNOTES',
      hint: 'tap: detail · 2x tap: back',
      selectable: true,
      items: [
        '[4m] ● Product standup',
        '[2h] ● Design walkthrough · notes',
        '[2h] Ask Taylor about the design review',
        '[1d] Idea: quiet morning briefing mode',
      ],
    },
    // A recording's note page (capabilities._show_voice_note_detail), sent
    // whole in the reader template for the glasses to scroll: its title,
    // what it is, a blank line, then each section a blank line apart.
    'quicknote-detail': {
      title: 'QUICKNOTES · NOTE',
      hint: 'tap: reminders · 2x tap: back',
      selectable: true,
      items: readerRows([], [
        'Product standup', 'Recording · 2 transcript sections', '',
        'OVERVIEW',
        'The team agreed to ship the beta on Friday. QA owns the final pass; Evan drafts the release notes tonight.',
        '', 'KEY POINTS',
        '– Ship the beta on Friday',
        '– QA signs off Thursday',
        '', 'NEXT STEPS',
        '□ Email release notes to the team',
        '□ Book the retro room',
        '', 'Say: ask the last note …',
        '', 'TRANSCRIPT',
        'Morning everyone. Quick one today, the beta is the only thing on the list.',
        'QA finished the regression pass last night; two minor issues, both fixed this morning.',
        'So the plan is Friday. QA owns the final pass on Thursday, and Evan drafts the release notes tonight.',
        'Someone needs to book the retro room for next week, and the release notes go to the whole team.',
        'Anything else? No? Then that is it, thanks all.',
      ].join('\n')),
    },
    // Live capture owns the page; the exclusive microphone belongs to the
    // recording, so these tappable rows are the wearer's only controls.
    'quicknote-recording': {
      title: 'QUICKNOTES · RECORDING',
      hint: 'Product standup · tap: choose',
      selectable: true,
      items: ['Save and finish', 'Pause recording', 'Discard recording'],
    },
    'quicknote-cues': {
      title: 'QUICKNOTES · AI CUES',
      hint: 'tap: open · left x2: back',
      selectable: true,
      items: [
        'Prep notes · Northwind v2 launch',
        'Answer · Canary rollout window',
        'Suggestion · Ask who owns QA sign-off',
        'Concept · Canary rollout',
      ],
    },
    'quicknote-cue': {
      title: 'QUICKNOTES · CUE',
      hint: 'left x2: back',
      selectable: false,
      items: [
        'ANSWER · now',
        'Canary rollout window',
        '',
        'Prep says staging soak finishes Thursday; the production rollout opens Friday at 09:00 UTC.',
      ],
    },
    'quicknote-prep': {
      title: 'QUICKNOTES · PREP',
      hint: 'left x2: back',
      selectable: false,
      items: [
        'PREP NOTES',
        'Northwind v2 launch',
        '',
        '1. Rollout window: Fri Mar 14, 09:00–11:00 UTC',
        '2. 18 API routes migrated; 2 legacy routes deprecated',
        '3. Rate-limit tier needs infra sign-off by Wednesday',
        '4. Latency target: p95 under 120 ms',
        '5. Ask: who owns post-deploy smoke checks?',
        '6. Risk: mobile clients on cached v1 schema',
        '7. Bring the canary checklist and rollback runbook',
      ],
    },
    'quicknote-discard': {
      title: 'QUICKNOTES · DISCARD?',
      hint: 'Product standup · tap: choose',
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
      hint: 'tap: detail · left x2: back',
      selectable: true,
      items: [
        'Calendar create · Succeeded',
        'Reminders create · Uncertain',
        'Message reply · Rejected',
      ],
    },
    'activity-detail': {
      title: 'ACTIVITY · DETAIL',
      hint: 'left x2: back',
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
 * REAL producer — `Headline.row`, `SearchResult.row`, `HudPanelPomodoro`,
 * `planner_rows` and `PanelPublisher._send_reader` in apps/mac/joat/services — so the
 * simulator proves a screen the Mac actually sends, not an invented one. */
export const PANEL_VISUAL_STATES = [
  'panel-planner',
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
  if (state.startsWith('panel-planner')) return 'planner';
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
      // `planner_rows`: "[word] text", the word being Late, a time, Today,
      // Tmrw or a weekday; more than eight ends in "+N more".
      kind: 'hud.panel',
      tab: 'planner',
      revision: 1,
      rows: [
        '[Late] Send the Q3 invoice',
        '[09:00] Call the bank',
        '[Today] Back up the staging database',
        '[14:30] Review the dashboard screenshots',
        '[Tmrw] Run the release smoke suite',
        '[Fri] Renew the domain',
        '[Sat] Sort out the input text',
        '+3 more',
      ],
    },
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

/** An opened headline, as PanelPublisher sends it: no list rows, the
 * metadata then the whole article as `body`, read as one continuous
 * document. Written for the fixture; long enough to cross many screens. */
export function panelArticleFixture(): { title: string; items: string[]; body: string; hint: string } {
  return {
    title: 'NEWS · ARTICLE',
    items: [],
    // The article template (PanelPublisher): headline, source and age, a
    // blank line, then the paragraphs a blank line apart.
    body: [
      'Consumer sentiment rises to 48.9\nFixture Wire · 12m',
      'Consumer sentiment rose slightly this month, reaching 48.9 after four consecutive months of decline, according to a survey published on Tuesday. The reading remains well below its long-run average, and respondents continued to cite prices and borrowing costs as their main concerns.',
      'Economists said the small improvement was consistent with a labour market that is cooling without breaking, and with fuel prices that have eased since the summer. Households with incomes in the top third reported the largest gains in confidence, while lower-income respondents were broadly unchanged.',
      'Expectations for inflation over the next year fell to 3.1 percent from 3.4 percent, the lowest reading since the spring. Longer-run expectations held steady at 2.9 percent. Analysts watch these figures closely because they can influence how businesses set prices and how workers negotiate wages.',
      'Spending plans were mixed. Fewer respondents said it was a good time to buy large household items such as appliances and furniture, citing high prices, while plans to buy a vehicle improved for a second month as dealers offered more incentives.',
      'The survey director said the results showed consumers were cautious rather than pessimistic. Many respondents mentioned the coming holiday season, and a growing share said they intended to keep spending close to last year’s level rather than cut back.',
      'Retailers will be watching the next two readings carefully. Holiday sales account for a large share of annual revenue for many stores, and several chains have already said they expect shoppers to look harder for discounts this year.',
      'The final reading for the month is due in two weeks.',
    ].join('\n\n'),
    hint: 'left x2: back',
  };
}
