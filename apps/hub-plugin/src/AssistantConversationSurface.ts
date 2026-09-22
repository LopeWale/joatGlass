import {
  TextContainerUpgrade,
  type RebuildPageContainer,
} from '@evenrealities/even_hub_sdk';
import { getTextWidth } from '@evenrealities/pretext';
import { GesturePolicy } from './GesturePolicy';
import type { BridgeClient } from './BridgeClient';
import {
  ASSISTANT_CONVERSATION_GEOMETRY,
  AssistantConversationContainerId,
  type AssistantConversationPage,
  type HudPageBuilder,
} from './HudPageBuilder';
import { TEXT_CONTAINER_MAX_BYTES, fitUtf8 } from './FirmwareLimits';
import type {
  AssistantConversationSnapshot,
  AssistantConversationTurn,
} from './types';
import {
  ASSISTANT_CONVERSATION_VISUAL_GEOMETRY,
  type AssistantConversationVisual,
} from './HudVisualSurface';

export type {
  AssistantConversationSnapshot,
  AssistantConversationState,
  AssistantConversationTurn,
} from './types';

export type ConversationLineRole = AssistantConversationTurn['role'] | 'separator';
export interface ConversationLine {
  role: ConversationLineRole;
  text: string;
}

export interface ConversationViewport extends AssistantConversationPage {
  visibleLines: ConversationLine[];
  lineCapacity: number;
  totalLines: number;
  scrollFromTail: number;
  maximumScrollFromTail: number;
}

export interface AssistantConversationIdentity {
  conversationId: string;
  revision: number;
}

interface AssistantConversationSurfaceOptions {
  indicatorIntervalMs?: number;
  continuationPromptEnabled?: boolean;
  paintView?: (
    view: AssistantConversationVisual,
    isCurrent: () => boolean,
  ) => Promise<boolean>;
  measureTextWidth?: (text: string) => number;
  /** Own the page-type transition when a visual HUD is mounted underneath.
   * Native text must never rebuild while an image upload is still in flight. */
  replacePage?: (page: RebuildPageContainer) => Promise<boolean>;
}

export type ConversationExitResult = 'inactive' | 'prompted' | 'exited' | 'failed';

export interface AssistantConversationSurfaceState {
  snapshot: AssistantConversationSnapshot;
  scrollFromTail: number;
  continuePrompt: boolean;
  indicatorFrame: number;
}

// Native text is the latency path. Keep one stable container geometry for the
// whole visit so thinking, streamed answer chunks, scrolling, and the
// continuation prompt use flicker-free textContainerUpgrade calls. Changing
// the card height as lines arrive would turn every early chunk into a full
// rebuildPageContainer and recreate the very redraw problem this path fixes.
const NATIVE_LINE_STAGES = [7] as const;
const VISUAL_LINE_STAGES = [1, 2, 3, 4, 5, 6, 7, 8, 9] as const;
// The visual renderer has eight observed morph phases. The text-only
// compatibility page cannot reproduce their geometry, but it keeps the same
// cadence and phase count with safe firmware glyphs.
const INDICATOR_FRAMES = [
  '■ □', '□ ■', '■', '■ ■', '□ □', '■', '□', '■',
] as const;
const ANSWERING_INDICATOR_MIN_MS = 2_000;
// Use the same product word as every other surface. "x2 exits" described the
// mechanism but made this page sound like it was closing JOAT; the gesture
// actually returns to the retained feature underneath.
const CONTINUE_COPY = 'Continue? tap: stay · double-click: back';

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function codePoints(value: string): string[] {
  return Array.from(value);
}

type TextMeasure = (text: string) => number;

interface ConversationViewportOptions {
  lineStages?: readonly number[];
  textWidth?: number;
  measureTextWidth?: TextMeasure;
}

function breakLongWord(word: string, width: number, measureTextWidth: TextMeasure): string[] {
  const points = codePoints(word);
  const lines: string[] = [];
  let line = '';
  for (const point of points) {
    const candidate = `${line}${point}`;
    if (line && measureTextWidth(candidate) > width) {
      lines.push(line);
      line = point;
    } else {
      line = candidate;
    }
  }
  if (line) lines.push(line);
  return lines.length > 0 ? lines : [' '];
}

/** Word-wrap with the installed firmware metrics, preserving explicit
 * paragraph boundaries and never splitting a UTF-16 surrogate pair. */
export function wrapConversationText(
  text: string,
  width: number = ASSISTANT_CONVERSATION_GEOMETRY.transcriptWidth,
  measureTextWidth: TextMeasure = getTextWidth,
): string[] {
  const paragraphs = text.replace(/\r\n?/g, '\n').split('\n');
  const lines: string[] = [];
  for (const paragraph of paragraphs) {
    const words = paragraph.trim().split(/\s+/u).filter(Boolean);
    if (words.length === 0) {
      lines.push(' ');
      continue;
    }
    let line = '';
    for (const word of words) {
      const candidate = line ? `${line} ${word}` : word;
      if (measureTextWidth(candidate) <= width) {
        line = candidate;
        continue;
      }
      if (line) {
        lines.push(line);
        line = '';
      }
      if (measureTextWidth(word) <= width) {
        line = word;
        continue;
      }
      const pieces = breakLongWord(word, width, measureTextWidth);
      lines.push(...pieces.slice(0, -1));
      line = pieces[pieces.length - 1] ?? '';
    }
    if (line) lines.push(line);
  }
  return lines.length > 0 ? lines : [' '];
}

/** All logical rows. A blank separator appears only between completed
 * assistant/user pairs, matching the native Even AI image hierarchy. */
export function conversationLines(
  snapshot: AssistantConversationSnapshot,
  options: ConversationViewportOptions = {},
): ConversationLine[] {
  const lines: ConversationLine[] = [];
  if (snapshot.earlierTurnsOmitted) {
    lines.push({ role: 'user', text: 'Earlier conversation…' });
  }
  for (const turn of snapshot.turns) {
    if (turn.role === 'user' && lines.some((line) => line.role === 'assistant')) {
      lines.push({ role: 'separator', text: ' ' });
    }
    for (const text of wrapConversationText(
      turn.text,
      options.textWidth ?? ASSISTANT_CONVERSATION_GEOMETRY.transcriptWidth,
      options.measureTextWidth ?? getTextWidth,
    )) {
      lines.push({ role: turn.role, text });
    }
  }
  return lines.length > 0 ? lines : [{ role: 'separator', text: ' ' }];
}

function stageFor(totalLines: number, stages: readonly number[]): number {
  const maximum = stages[stages.length - 1]!;
  const needed = clamp(totalLines, 1, maximum);
  return stages.find((stage) => stage >= needed) ?? maximum;
}

function centered(text: string, width: number): string {
  const room = Math.max(0, width - getTextWidth(text));
  const space = Math.max(1, getTextWidth(' '));
  return `${' '.repeat(Math.floor(room / space / 2))}${text}`;
}

function scrollbar(total: number, capacity: number, scrollFromTail: number): string {
  if (total <= capacity) return ' ';
  const slots = capacity;
  const thumbSize = Math.max(1, Math.round((capacity / total) * slots));
  const maximum = total - capacity;
  const startFromTop = maximum - scrollFromTail;
  const travel = Math.max(0, slots - thumbSize);
  const thumbStart = maximum === 0 ? 0 : Math.round((startFromTop / maximum) * travel);
  return Array.from({ length: slots }, (_, index) => (
    index >= thumbStart && index < thumbStart + thumbSize ? '█' : '│'
  )).join('\n');
}

export function conversationViewport(
  snapshot: AssistantConversationSnapshot,
  requestedScrollFromTail = 0,
  indicatorFrame = 0,
  continuePrompt = false,
  options: ConversationViewportOptions = {},
): ConversationViewport {
  const all = conversationLines(snapshot, options);
  const lineCapacity = stageFor(all.length, options.lineStages ?? NATIVE_LINE_STAGES);
  const maximumScrollFromTail = Math.max(0, all.length - lineCapacity);
  const scrollFromTail = clamp(
    Number.isFinite(requestedScrollFromTail) ? Math.floor(requestedScrollFromTail) : 0,
    0,
    maximumScrollFromTail,
  );
  const start = Math.max(0, all.length - lineCapacity - scrollFromTail);
  const visibleLines = all.slice(start, start + lineCapacity);
  while (visibleLines.length < lineCapacity) {
    visibleLines.push({ role: 'separator', text: ' ' });
  }
  const dimText = visibleLines
    .map((line) => line.role === 'user' ? line.text : ' ')
    .join('\n');
  const brightText = visibleLines
    .map((line) => line.role === 'assistant' ? line.text : ' ')
    .join('\n');
  const geometry = ASSISTANT_CONVERSATION_GEOMETRY;
  const cardHeight = (
    lineCapacity * geometry.lineHeight
    + geometry.cardTopPadding
    + geometry.footerGap
    + geometry.lineHeight
    + geometry.footerBottomInset
  );
  const footer = continuePrompt
    ? CONTINUE_COPY
    : snapshot.state === 'ready'
      ? '■'
      : INDICATOR_FRAMES[indicatorFrame % INDICATOR_FRAMES.length]!;
  return {
    cardTop: geometry.cardBottom - cardHeight,
    cardHeight,
    transcriptTop: geometry.cardBottom - cardHeight + geometry.cardTopPadding,
    transcriptHeight: lineCapacity * geometry.lineHeight,
    dimText,
    brightText,
    scrollbarText: scrollbar(all.length, lineCapacity, scrollFromTail),
    footerText: centered(GesturePolicy.formatHint(footer), ASSISTANT_CONVERSATION_GEOMETRY.footerWidth),
    visibleLines,
    lineCapacity,
    totalLines: all.length,
    scrollFromTail,
    maximumScrollFromTail,
  };
}

export function validAssistantConversationSnapshot(value: AssistantConversationSnapshot): boolean {
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(value.conversationId)) return false;
  if (!Number.isSafeInteger(value.revision) || value.revision < 0) return false;
  if (!['thinking', 'answering', 'ready'].includes(value.state)) return false;
  if (!Number.isInteger(value.continueAfterMs) || value.continueAfterMs < 1_000 || value.continueAfterMs > 120_000) {
    return false;
  }
  if (!Array.isArray(value.turns) || value.turns.length < 1 || value.turns.length > 16) return false;
  let units = 0;
  for (const turn of value.turns) {
    if ((turn.role !== 'user' && turn.role !== 'assistant') || !turn.text || turn.text.length > 4_000) return false;
    units += turn.text.length;
  }
  return units <= 16_000 && typeof value.earlierTurnsOmitted === 'boolean';
}

function copySnapshot(value: AssistantConversationSnapshot): AssistantConversationSnapshot {
  return { ...value, turns: value.turns.map((turn) => ({ ...turn })) };
}

/**
 * Owns one retained conversation surface. New Mac revisions are coalesced to
 * one latest snapshot while a bridge update is in flight. Production uses the
 * native-container path so revisions and gestures do not incur image uploads.
 * Scroll position commits only after the complete aligned dim/bright viewport
 * has been accepted.
 */
export class AssistantConversationSurface {
  private snapshot?: AssistantConversationSnapshot;
  private retired = new Set<string>();
  private active = false;
  private paused = false;
  private exiting = false;
  private continuePrompt = false;
  private desiredScroll = 0;
  private committedScroll = 0;
  private renderedRevision = -1;
  private pageLineCapacity = 0;
  private lastView?: ConversationViewport;
  private indicatorFrame = 0;
  private indicatorTimer?: ReturnType<typeof setTimeout>;
  private continuationTimer?: ReturnType<typeof setTimeout>;
  private snapshotDirty = false;
  private footerDirty = false;
  private draining?: Promise<void>;
  private lastPaintSucceeded = false;
  private readonly indicatorIntervalMs: number;
  private readonly continuationPromptEnabled: boolean;
  private readonly paintView?: AssistantConversationSurfaceOptions['paintView'];
  private readonly replacePage?: AssistantConversationSurfaceOptions['replacePage'];
  private readonly viewportOptions: ConversationViewportOptions;

  constructor(
    private readonly client: Pick<BridgeClient, 'require'>,
    private readonly layouts: HudPageBuilder,
    private readonly restore: () => Promise<boolean>,
    options: AssistantConversationSurfaceOptions = {},
  ) {
    this.indicatorIntervalMs = Math.max(0, options.indicatorIntervalMs ?? 450);
    this.continuationPromptEnabled = options.continuationPromptEnabled ?? true;
    this.paintView = options.paintView;
    this.replacePage = options.replacePage;
    this.viewportOptions = options.paintView
      ? {
        lineStages: VISUAL_LINE_STAGES,
        textWidth: ASSISTANT_CONVERSATION_VISUAL_GEOMETRY.width
          - ASSISTANT_CONVERSATION_VISUAL_GEOMETRY.textInsetLeft
          - ASSISTANT_CONVERSATION_VISUAL_GEOMETRY.textInsetRight,
        ...(options.measureTextWidth ? { measureTextWidth: options.measureTextWidth } : {}),
      }
      : {};
  }

  async present(value: AssistantConversationSnapshot): Promise<boolean> {
    if (!validAssistantConversationSnapshot(value) || this.retired.has(value.conversationId)
      || this.exiting || this.paused) return false;
    const current = this.snapshot;
    const sameConversation = current?.conversationId === value.conversationId;
    if (sameConversation && value.revision < current.revision) return false;
    if (sameConversation && value.revision === current.revision && this.active) {
      return true;
    }
    const entering = !this.active || !sameConversation;
    if (entering && current && !sameConversation) this.retired.add(current.conversationId);
    this.clearTimers();
    this.active = true;
    this.paused = false;
    this.continuePrompt = false;
    this.snapshot = copySnapshot(value);
    if (entering) {
      // The previous page now belongs to some other surface. A resumed
      // conversation must rebuild its containers even when the Mac revision
      // is unchanged.
      this.pageLineCapacity = 0;
      this.lastView = undefined;
    }
    if (entering || this.committedScroll === 0) {
      this.desiredScroll = 0;
      this.committedScroll = 0;
    } else if (current) {
      const oldLines = conversationLines(current, this.viewportOptions).length;
      const newLines = conversationLines(value, this.viewportOptions).length;
      this.desiredScroll = Math.max(0, this.committedScroll + newLines - oldLines);
    }
    this.snapshotDirty = true;
    const work = this.ensureDrain();
    if (!entering) return true;
    await work;
    return this.active && this.renderedRevision >= value.revision && this.lastPaintSucceeded;
  }

  isActive(): boolean { return this.active; }
  isContinuePromptVisible(): boolean { return this.canInteract() && this.continuePrompt; }
  currentRevision(): number { return this.renderedRevision; }
  scrollFromTail(): number { return this.committedScroll; }
  currentIdentity(): AssistantConversationIdentity | undefined {
    const current = this.snapshot;
    if (!this.canInteract() || !current) return undefined;
    return { conversationId: current.conversationId, revision: current.revision };
  }

  /** JSON-safe visible state for Even Hub's foreground/headless WebView handoff. */
  exportState(): AssistantConversationSurfaceState | undefined {
    const current = this.snapshot;
    if (!this.active || !current) return undefined;
    return {
      snapshot: copySnapshot(current),
      scrollFromTail: this.committedScroll,
      continuePrompt: this.continuePrompt,
      indicatorFrame: this.indicatorFrame,
    };
  }

  /** Adopt a saved native page without painting. The caller owns the one
   * page rebuild after all background state has been imported. */
  importState(value: unknown): boolean {
    if (typeof value !== 'object' || value === null) return false;
    const saved = value as Partial<AssistantConversationSurfaceState>;
    const snapshot = saved.snapshot;
    if (!snapshot || !validAssistantConversationSnapshot(snapshot)) return false;
    if (!Number.isSafeInteger(saved.scrollFromTail) || (saved.scrollFromTail ?? -1) < 0) return false;
    if (typeof saved.continuePrompt !== 'boolean'
      || !Number.isSafeInteger(saved.indicatorFrame) || (saved.indicatorFrame ?? -1) < 0) return false;
    const scrollFromTail = saved.scrollFromTail!;
    const indicatorFrame = saved.indicatorFrame!;
    this.clearTimers();
    this.snapshot = copySnapshot(snapshot);
    this.active = true;
    this.paused = false;
    this.exiting = false;
    this.continuePrompt = saved.continuePrompt;
    this.desiredScroll = scrollFromTail;
    this.committedScroll = scrollFromTail;
    this.indicatorFrame = indicatorFrame % INDICATOR_FRAMES.length;
    this.renderedRevision = -1;
    this.pageLineCapacity = 0;
    this.lastView = undefined;
    this.snapshotDirty = false;
    this.footerDirty = false;
    this.lastPaintSucceeded = false;
    return true;
  }

  scroll(direction: 'up' | 'down'): boolean {
    const current = this.snapshot;
    if (!this.canInteract() || !current || this.continuePrompt) return false;
    const view = conversationViewport(
      current, this.desiredScroll, this.indicatorFrame, false, this.viewportOptions,
    );
    const candidate = clamp(
      this.desiredScroll + (direction === 'up' ? 1 : -1),
      0,
      view.maximumScrollFromTail,
    );
    if (candidate === this.desiredScroll) return false;
    this.desiredScroll = candidate;
    this.snapshotDirty = true;
    void this.ensureDrain();
    return true;
  }

  async continueConversation(): Promise<boolean> {
    if (!this.isContinuePromptVisible()) return false;
    this.continuePrompt = false;
    this.footerDirty = true;
    await this.ensureDrain();
    if (!this.lastPaintSucceeded) {
      // The wearer still sees the question. Keep local authority aligned
      // with the pixels and do not tell the Mac that Continue was accepted.
      this.continuePrompt = true;
      this.footerDirty = true;
      void this.ensureDrain();
      return false;
    }
    this.armContinuationTimer();
    return true;
  }

  async requestExit(): Promise<ConversationExitResult> {
    if (!this.canInteract()) return 'inactive';
    if (!this.continuePrompt) {
      this.continuePrompt = true;
      this.clearTimers();
      this.footerDirty = true;
      await this.ensureDrain();
      return this.lastPaintSucceeded ? 'prompted' : 'failed';
    }
    return this.exit();
  }

  async dismiss(conversationId: string, revision: number): Promise<boolean> {
    const current = this.snapshot;
    if (!current || current.conversationId !== conversationId || revision !== current.revision) return false;
    return (await this.exit()) === 'exited';
  }

  /** Quiesce direct native writes while the presenter replaces the page.
   * The snapshot stays resumable because a feature handoff is not a wearer
   * decision to end their conversation. */
  async pauseForHandoff(): Promise<boolean> {
    if (!this.canInteract()) return false;
    this.paused = true;
    this.clearTimers();
    this.snapshotDirty = false;
    this.footerDirty = false;
    await this.whenIdle();
    return this.active && this.paused && !this.exiting;
  }

  completeHandoff(): boolean {
    if (!this.active || !this.paused || this.exiting) return false;
    this.active = false;
    this.paused = false;
    this.continuePrompt = false;
    this.pageLineCapacity = 0;
    this.lastView = undefined;
    return true;
  }

  async resumeAfterFailedHandoff(): Promise<boolean> {
    if (!this.active || !this.paused || this.exiting || !this.snapshot) return false;
    this.paused = false;
    this.pageLineCapacity = 0;
    this.lastView = undefined;
    this.snapshotDirty = true;
    await this.ensureDrain();
    return this.lastPaintSucceeded;
  }

  /** A higher-priority surface owns the page now. Retire the conversation
   * without restoring the page it is about to replace. */
  retireForHandoff(): boolean {
    const current = this.snapshot;
    if (!this.isActive() || !current) return false;
    this.clearTimers();
    this.retired.add(current.conversationId);
    this.active = false;
    this.paused = false;
    this.snapshot = undefined;
    this.snapshotDirty = false;
    this.footerDirty = false;
    this.lastView = undefined;
    this.pageLineCapacity = 0;
    return true;
  }

  async whenIdle(): Promise<void> {
    for (;;) {
      const current = this.draining;
      if (!current) return;
      await current;
      if (this.draining === current) return;
    }
  }

  async repaint(): Promise<boolean> {
    const current = this.snapshot;
    if (!this.canWrite() || !current) return false;
    this.pageLineCapacity = 0;
    this.lastView = undefined;
    this.snapshotDirty = true;
    await this.ensureDrain();
    return this.renderedRevision >= current.revision && this.lastPaintSucceeded;
  }

  private async exit(): Promise<ConversationExitResult> {
    const current = this.snapshot;
    if (!current || !this.canInteract()) return 'inactive';
    this.exiting = true;
    this.clearTimers();
    this.snapshotDirty = false;
    this.footerDirty = false;
    await this.whenIdle();
    const restored = await this.restore().catch(() => false);
    if (!restored) {
      this.exiting = false;
      this.continuePrompt = true;
      this.pageLineCapacity = 0;
      this.lastView = undefined;
      this.snapshotDirty = true;
      void this.ensureDrain();
      return 'failed';
    }
    this.retired.add(current.conversationId);
    this.active = false;
    this.paused = false;
    this.exiting = false;
    this.snapshot = undefined;
    this.continuePrompt = false;
    this.pageLineCapacity = 0;
    this.lastView = undefined;
    this.desiredScroll = 0;
    this.committedScroll = 0;
    return 'exited';
  }

  private ensureDrain(): Promise<void> {
    if (this.draining) return this.draining;
    const work = this.drain();
    const tracked = work.finally(() => {
      if (this.draining !== tracked) return;
      this.draining = undefined;
      if ((this.snapshotDirty || this.footerDirty) && this.canWrite()) void this.ensureDrain();
    });
    this.draining = tracked;
    return tracked;
  }

  private async drain(): Promise<void> {
    while (this.canWrite() && (this.snapshotDirty || this.footerDirty)) {
      if (this.snapshotDirty) {
        this.snapshotDirty = false;
        this.footerDirty = false;
        this.lastPaintSucceeded = await this.paintSnapshot();
      } else {
        this.footerDirty = false;
        this.lastPaintSucceeded = await this.paintFooter();
      }
    }
  }

  private async paintSnapshot(): Promise<boolean> {
    const current = this.snapshot;
    if (!current || !this.canWrite()) return false;
    const requestedScroll = this.desiredScroll;
    const view = conversationViewport(
      current, requestedScroll, this.indicatorFrame, this.continuePrompt,
      this.viewportOptions,
    );
    const needsRebuild = this.pageLineCapacity !== view.lineCapacity || this.lastView === undefined;
    let painted = this.paintView
      ? await this.paintVisual(current, view)
      : needsRebuild
        ? await this.rebuild(view)
        : await this.upgradeView(view);
    if (!painted && !this.paintView && !needsRebuild && this.canWrite()) {
      painted = await this.rebuild(view);
    }
    if (!painted) {
      this.desiredScroll = this.committedScroll;
      return false;
    }
    this.pageLineCapacity = view.lineCapacity;
    this.lastView = view;
    if (this.snapshot === current && this.desiredScroll === requestedScroll) {
      this.committedScroll = view.scrollFromTail;
      this.renderedRevision = current.revision;
      this.armStateTimers();
    }
    return true;
  }

  private async paintFooter(): Promise<boolean> {
    const current = this.snapshot;
    if (!current || !this.canWrite() || !this.lastView) return false;
    const view = conversationViewport(
      current, this.committedScroll, this.indicatorFrame, this.continuePrompt,
      this.viewportOptions,
    );
    const ok = this.paintView
      ? await this.paintVisual(current, view)
      : await this.upgrade(
        AssistantConversationContainerId.Generation,
        'generation',
        view.footerText,
        this.lastView.footerText,
      );
    if (ok) {
      this.lastView = { ...this.lastView, footerText: view.footerText };
      this.armStateTimers();
    }
    return ok;
  }

  private rebuild(view: ConversationViewport): Promise<boolean> {
    const page = this.layouts.assistantConversationLayout(view);
    return this.replacePage
      ? this.replacePage(page)
      : this.client.require().rebuildPageContainer(page);
  }

  private paintVisual(
    snapshot: AssistantConversationSnapshot,
    view: ConversationViewport,
  ): Promise<boolean> {
    const paint = this.paintView;
    if (!paint) return Promise.resolve(false);
    const indicatorFrame = this.indicatorFrame;
    const continuePrompt = this.continuePrompt;
    const expectedScroll = view.scrollFromTail;
    const visual: AssistantConversationVisual = {
      conversationId: snapshot.conversationId,
      revision: snapshot.revision,
      state: snapshot.state,
      visibleLines: view.visibleLines.map((line) => ({ ...line })),
      lineCapacity: view.lineCapacity,
      totalLines: view.totalLines,
      scrollFromTail: view.scrollFromTail,
      maximumScrollFromTail: view.maximumScrollFromTail,
      indicatorFrame,
      continuePrompt,
    };
    return paint(visual, () => (
      this.canWrite()
      && this.snapshot === snapshot
      && this.snapshot.revision === snapshot.revision
      && this.desiredScroll === expectedScroll
      && this.indicatorFrame === indicatorFrame
      && this.continuePrompt === continuePrompt
    ));
  }

  private async upgradeView(view: ConversationViewport): Promise<boolean> {
    const prior = this.lastView;
    const updates: Array<[number, string, string, string]> = [];
    if (prior?.dimText !== view.dimText) {
      updates.push([AssistantConversationContainerId.WearerTurns, 'wearer-turns', view.dimText, prior?.dimText ?? '']);
    }
    if (prior?.brightText !== view.brightText) {
      updates.push([AssistantConversationContainerId.JoatTurns, 'joat-turns', view.brightText, prior?.brightText ?? '']);
    }
    if (prior?.scrollbarText !== view.scrollbarText) {
      updates.push([AssistantConversationContainerId.ScrollPosition, 'scroll-position', view.scrollbarText, prior?.scrollbarText ?? '']);
    }
    if (prior?.footerText !== view.footerText) {
      updates.push([AssistantConversationContainerId.Generation, 'generation', view.footerText, prior?.footerText ?? '']);
    }
    for (const [id, name, content, previous] of updates) {
      if (!await this.upgrade(id, name, content, previous)) return false;
    }
    return true;
  }

  private upgrade(
    containerID: number,
    containerName: string,
    content: string,
    previous = '',
  ): Promise<boolean> {
    const replacement = fitUtf8(content, TEXT_CONTAINER_MAX_BYTES) || ' ';
    return this.client.require().textContainerUpgrade(new TextContainerUpgrade({
      containerID,
      containerName,
      contentOffset: 0,
      contentLength: Math.max(previous.length, replacement.length),
      content: replacement,
    }));
  }

  private clearTimers(): void {
    if (this.indicatorTimer !== undefined) clearTimeout(this.indicatorTimer);
    if (this.continuationTimer !== undefined) clearTimeout(this.continuationTimer);
    this.indicatorTimer = undefined;
    this.continuationTimer = undefined;
  }

  private armStateTimers(): void {
    this.clearTimers();
    const state = this.snapshot?.state;
    // During silent thinking the local glyph moves promptly. Once answer text
    // is streaming, each new snapshot is already visible progress and resets
    // this timer; wait longer before spending the serialized image lane on a
    // decorative tick. A stalled stream still gets an activity update.
    const interval = state === 'thinking'
      ? this.indicatorIntervalMs
      : state === 'answering' && this.indicatorIntervalMs > 0
        ? Math.max(ANSWERING_INDICATOR_MIN_MS, this.indicatorIntervalMs)
        : 0;
    if (interval > 0) {
      this.indicatorTimer = setTimeout(() => {
        this.indicatorTimer = undefined;
        if (!this.canWrite() || this.continuePrompt) return;
        this.indicatorFrame = (this.indicatorFrame + 1) % INDICATOR_FRAMES.length;
        this.footerDirty = true;
        void this.ensureDrain();
      }, interval);
      return;
    }
    this.armContinuationTimer();
  }

  private armContinuationTimer(): void {
    if (!this.continuationPromptEnabled || this.snapshot?.state !== 'ready' || this.continuePrompt) return;
    if (this.continuationTimer !== undefined) clearTimeout(this.continuationTimer);
    this.continuationTimer = setTimeout(() => {
      this.continuationTimer = undefined;
      if (!this.canWrite() || this.snapshot?.state !== 'ready') return;
      this.continuePrompt = true;
      this.footerDirty = true;
      void this.ensureDrain();
    }, this.snapshot.continueAfterMs);
  }

  private canWrite(): boolean {
    return this.active && !this.paused && !this.exiting;
  }

  private canInteract(): boolean {
    return this.canWrite();
  }
}
