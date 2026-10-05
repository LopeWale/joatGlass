import {
  TextContainerUpgrade,
  type RebuildPageContainer,
} from '@evenrealities/even_hub_sdk';
import { pxTruncate } from '@evenrealities/pretext';
import { firmwareLineWidth } from './FirmwareText';
import type { BridgeClient } from './BridgeClient';
import { TerminalConversationViewport } from './TerminalConversation';
import { wrapTerminalParagraph } from './TerminalText';
import type { BrainstormHudModel, TerminalConversationMessage } from './types';

/** Page-local identities. The layout callback must use these exact values so
 * every update addresses the same native container created on entry. */
export const NativeTerminalContainer = {
  Head: { id: 16, name: 'terminal-head' },
  Body: { id: 17, name: 'terminal-body' },
} as const;

/** The fixed page fits one firmware line above nine body lines on 576x288.
 * Inner dimensions subtract the four-pixel padding on both sides. */
export const NATIVE_TERMINAL_GEOMETRY = {
  width: 576,
  headY: 0,
  headHeight: 35,
  bodyY: 35,
  bodyHeight: 253,
  padding: 4,
  bodyTextWidth: 568,
  bodyLineCapacity: 9,
  lineHeight: 27,
} as const;

const BODY_FIXED_ROWS = 2; // provider/state + truthful gesture footer
const MAX_PROMPT_ROWS = NATIVE_TERMINAL_GEOMETRY.bodyLineCapacity - BODY_FIXED_ROWS - 1;
const MAX_LINE_UNITS = 190;
const MAX_UPGRADE_UNITS = 2_000;
const HEAD_TEXT = 'JOAT · AGENT TERMINAL';
const BRAINSTORM_HEAD_TEXT = 'JOAT · BRAINSTORM';

/** Brainstorm (Codex proposes, Claude critiques) shares this page with the
 * Agent Terminal: one head line, a status row, the transcript, a footer
 * naming the gestures the BrainstormController accepts. */
function isBrainstorm(model: Pick<BrainstormHudModel, 'screen'>): boolean {
  return model.screen === 'brainstorm';
}

function headTextFor(model: Pick<BrainstormHudModel, 'screen'>): string {
  return isBrainstorm(model) ? BRAINSTORM_HEAD_TEXT : HEAD_TEXT;
}

/** The gestures BrainstormController accepts in each state, in the raster
 * footer's words but measured for the firmware font: the raster running
 * hint also named the spoken "send that to hermes" verb and does not fit
 * one firmware line. Scrolled up, the row says how far from the live tail
 * the wearer is and what the double-press does there. */
function brainstormFooter(model: BrainstormHudModel, newer: number): string {
  if (newer > 0) {
    return `▼ ${newer} newer · swipe down · double-click: ${model.state === 'running' ? 'cancel' : 'menu'}`;
  }
  switch (model.state) {
    case 'running': return 'swipe: scroll · tap: terminal · double-click: cancel';
    case 'acceptance_ready': return 'tap: start test · double-click: menu';
    case 'topic_capture': return 'speak your question · double-click: cancel';
    case 'ready': return 'tap: speak · double-click: menu';
    default: return 'swipe: scroll · double-click: menu';
  }
}

/** The Brainstorm status row: its state, then who is speaking and the round
 * cost while it runs, else the question it is about. */
function brainstormStatusRow(model: BrainstormHudModel): string {
  const live = model.state === 'running' && model.activeSpeaker?.trim()
    ? model.activeSpeaker.trim()
    : model.topic.replace(/\s+/gu, ' ').trim();
  return live ? `${stateName(model)} · ${live}` : stateName(model);
}

export interface NativeTerminalPage {
  headText: string;
  bodyText: string;
  conversationKey: string;
  older: number;
  newer: number;
  state: BrainstormHudModel['state'];
  promptId?: string;
}

export type NativeTerminalLayoutFactory = (
  page: Readonly<NativeTerminalPage>,
) => RebuildPageContainer;

export interface NativeTerminalSurfaceOptions {
  measureTextWidth?: (text: string) => number;
}

export interface NativeTerminalSurfaceState {
  version: 1;
  viewport: ReturnType<TerminalConversationViewport['exportState']>;
}

interface TerminalPrompt {
  id: string;
  question: string;
  isCurrent: () => boolean;
}

interface DesiredTerminal {
  visit: number;
  token: number;
  model: BrainstormHudModel;
  status?: string;
  isCurrent: () => boolean;
}

interface RenderSnapshot {
  desired: DesiredTerminal;
  prompt?: TerminalPrompt;
  page: NativeTerminalPage;
}

function current(check: () => boolean): boolean {
  try { return check(); } catch { return false; }
}

function copyModel(model: BrainstormHudModel): BrainstormHudModel {
  return {
    ...model,
    collaborators: [...model.collaborators],
    messages: model.messages?.map((message) => ({ ...message })),
    terminalPrompt: model.terminalPrompt ? { ...model.terminalPrompt } : undefined,
  };
}

function focusName(topic: string): string {
  return ((topic.split(' · ')[0] ?? '').replace(/\s+/gu, ' ').trim() || 'AGENT').toUpperCase();
}

function stateName(model: BrainstormHudModel): string {
  if (model.taskRecovery) return 'CHECK TASK';
  if (model.inputRequired) return 'REPLY NEEDED';
  switch (model.state) {
    case 'unavailable': return 'UNAVAILABLE';
    case 'approval_required': return 'APPROVAL REQUIRED';
    case 'ready': return 'READY';
    case 'topic_capture': return 'LISTENING';
    case 'acceptance_ready': return 'READY TO START';
    case 'running': return 'WORKING';
    case 'settled': return 'COMPLETE';
    case 'cancelled': return 'CANCELLED';
    case 'failed': return 'FAILED';
    case 'blocked': return 'BLOCKED';
  }
}

function validQuestion(id: string, question: string): boolean {
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(id) || !question.trim()
    || question.length > 1_000 || question.includes('\0')) return false;
  for (let index = 0; index < question.length; index += 1) {
    const unit = question.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = question.charCodeAt(++index);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return false;
  }
  return true;
}

/** Metric wrapping also caps code units per line. The cap keeps pathological
 * zero-width sequences within the SDK's 2,000-character upgrade limit while
 * preserving them across subsequent rows. */
function wrapWithMetrics(
  text: string,
  measureTextWidth: (text: string) => number,
): string[] {
  return wrapTerminalParagraph(
    text,
    NATIVE_TERMINAL_GEOMETRY.bodyTextWidth,
    (candidate) => candidate.length > MAX_LINE_UNITS
      ? NATIVE_TERMINAL_GEOMETRY.bodyTextWidth + 1
      : measureTextWidth(candidate),
  );
}

function oneMeasuredLine(
  text: string,
  measureTextWidth: (text: string) => number,
): string {
  const codepointBounded = Array.from(text).slice(0, MAX_LINE_UNITS).join('');
  if (measureTextWidth(codepointBounded) <= NATIVE_TERMINAL_GEOMETRY.bodyTextWidth) {
    return codepointBounded;
  }
  return pxTruncate(codepointBounded, NATIVE_TERMINAL_GEOMETRY.bodyTextWidth);
}

function promptRows(
  prompt: TerminalPrompt | undefined,
  measureTextWidth: (text: string) => number,
): string[] | undefined {
  if (!prompt) return [];
  if (!validQuestion(prompt.id, prompt.question) || !current(prompt.isCurrent)) return;
  const paragraphs = prompt.question.replace(/\r\n?/g, '\n').split('\n');
  const rows: string[] = [];
  paragraphs.forEach((paragraph, index) => {
    const prefix = index === 0 ? 'QUESTION > ' : '';
    rows.push(...wrapWithMetrics(`${prefix}${paragraph}`, measureTextWidth));
  });
  return rows.length <= MAX_PROMPT_ROWS ? rows : undefined;
}

function messagesFor(model: BrainstormHudModel): readonly TerminalConversationMessage[] {
  if (model.messages !== undefined) return model.messages;
  const activity = model.activity.trim();
  return activity ? [{ id: '__activity', role: 'status', text: activity }] : [];
}

function scrollHint(older: number, newer: number): string[] {
  return [
    ...(older > 0 ? ['up: older'] : []),
    ...(newer > 0 ? ['down: newer'] : []),
  ];
}

/** Every hint corresponds to an action the current EventRouter and
 * BrainstormController can actually accept. Scroll directions are included
 * only when TerminalConversationViewport can move that way. */
export function nativeTerminalFooter(
  model: BrainstormHudModel,
  older: number,
  newer: number,
  promptVisible = false,
): string {
  if (promptVisible) return 'Listening · speak your answer · double-click: cancel';
  if ((model.approvals ?? 0) > 0) {
    return 'tap: approve · swipe: deny · double-click: back';
  }

  let action: string | undefined;
  if (model.taskRecovery) action = 'tap: check task status';
  else if (model.state === 'approval_required'
    || model.state === 'blocked'
    || model.state === 'unavailable') action = 'tap: retry';
  else if (model.state === 'ready') action = model.inputRequired ? 'tap: reply' : 'tap: speak';
  else if (model.state === 'running') action = 'say "terminal cancel"';

  const parts = [
    ...(action ? [action] : []),
    ...scrollHint(older, newer),
    'double-click: back',
  ];
  return oneMeasuredLine(parts.join(' · '), firmwareLineWidth);
}

function validLayout(layout: RebuildPageContainer, page: NativeTerminalPage): boolean {
  const text = layout.textObject ?? [];
  const head = text.find((item) => item.containerID === NativeTerminalContainer.Head.id
    && item.containerName === NativeTerminalContainer.Head.name);
  const body = text.find((item) => item.containerID === NativeTerminalContainer.Body.id
    && item.containerName === NativeTerminalContainer.Body.name);
  return layout.containerTotalNum === 2
    && text.length === 2
    && (layout.imageObject?.length ?? 0) === 0
    && (layout.listObject?.length ?? 0) === 0
    && text.filter((item) => item.isEventCapture === 1).length === 1
    && head?.isEventCapture === 0
    && body?.isEventCapture === 1
    && head.content === page.headText
    && body.content === page.bodyText;
}

/**
 * Owns the native Agent Terminal document. A visit installs one fixed page;
 * every subsequent model, prompt, and scroll revision replaces the complete
 * body in one serialized TextContainerUpgrade.
 */
export class NativeTerminalSurface {
  private readonly viewport = new TerminalConversationViewport();
  private readonly measureTextWidth: (text: string) => number;
  private desired?: DesiredTerminal;
  private prompt?: TerminalPrompt;
  private active = false;
  private paused = false;
  private pageMounted = false;
  private dirty = false;
  private nextToken = 0;
  private visit = 0;
  private renderedToken = 0;
  private renderedPromptId?: string;
  /** Last body acknowledged by the bridge, including a superseded write. It
   * remains the overwrite-length authority so stale tail text is cleared. */
  private physicalBodyText = '';
  /** Last head line acknowledged by the bridge, for the same reason. */
  private physicalHeadText = '';
  private lastPaintSucceeded = false;
  private draining?: Promise<void>;

  constructor(
    private readonly client: Pick<BridgeClient, 'require'>,
    private readonly layout: NativeTerminalLayoutFactory,
    private readonly replacePage: (page: RebuildPageContainer) => Promise<boolean>,
    options: NativeTerminalSurfaceOptions = {},
  ) {
    // A row is one firmware line when the firmware's own measurement
    // (leading spaces dropped, glyphs rounded one by one) fits the width.
    this.measureTextWidth = options.measureTextWidth ?? firmwareLineWidth;
  }

  async present(
    model: BrainstormHudModel,
    status?: string,
    isCurrent: () => boolean = () => true,
  ): Promise<boolean> {
    if ((model.screen !== 'agent_terminal' && !isBrainstorm(model))
      || !current(isCurrent) || this.paused) return false;
    const copy = copyModel(model);
    const key = this.conversationKey(copy);
    const entering = !this.active || this.desired === undefined;
    const conversationChanged = !entering
      && this.desired !== undefined
      && this.conversationKey(this.desired.model) !== key;
    if (entering) this.visit += 1;
    // The head line is the status header; a revision presented without one
    // keeps the line it has rather than reverting to the bare page name.
    const retainedStatus = !entering ? this.desired?.status : undefined;
    const nextStatus = status?.trim() ? status.replace(/\s+/gu, ' ').trim() : retainedStatus;
    const desired: DesiredTerminal = {
      visit: this.visit,
      token: ++this.nextToken,
      model: copy,
      ...(nextStatus ? { status: nextStatus } : {}),
      isCurrent,
    };
    this.desired = desired;
    this.active = true;
    this.paused = false;
    if (entering) {
      this.pageMounted = false;
      this.physicalBodyText = '';
      this.renderedPromptId = undefined;
      this.prompt = undefined;
    } else if (conversationChanged) {
      // A prompt belongs to one conversation. The native document geometry is
      // unchanged, so switching agents still needs only one body upgrade.
      this.prompt = undefined;
      this.renderedPromptId = undefined;
    }
    this.dirty = true;
    await this.ensureDrain();
    return this.desired === desired && this.renderedToken === desired.token
      && this.lastPaintSucceeded && current(isCurrent);
  }

  async showPrompt(
    id: string,
    question: string,
    isCurrent: () => boolean = () => true,
  ): Promise<boolean> {
    if (!this.canInteract() || !validQuestion(id, question) || !current(isCurrent)) return false;
    const candidate: TerminalPrompt = { id, question, isCurrent };
    if (promptRows(candidate, this.measureTextWidth) === undefined) return false;
    const existing = this.prompt;
    if (existing?.id === id) {
      return existing.question === question && this.renderedPromptId === id
        && this.lastPaintSucceeded && current(existing.isCurrent);
    }
    this.prompt = candidate;
    this.dirty = true;
    this.bumpDesired();
    await this.ensureDrain();
    return this.prompt === candidate && this.renderedPromptId === id
      && this.lastPaintSucceeded && current(isCurrent);
  }

  /** The head line is the page's status header (clock · page · mic ·
   * battery). A changed line is one head upgrade in the same serialized
   * lane as the body, so the clock keeps moving on a long terminal visit. */
  async setStatus(status: string): Promise<boolean> {
    if (!this.canInteract() || !this.desired) return false;
    const clean = status.replace(/\s+/gu, ' ').trim();
    if (!clean || clean === this.desired.status) return true;
    this.desired = { ...this.desired, status: clean, token: ++this.nextToken };
    this.dirty = true;
    await this.ensureDrain();
    return this.desired?.status === clean && this.lastPaintSucceeded;
  }

  async dismissPrompt(id: string): Promise<boolean> {
    if (!this.canInteract() || this.prompt?.id !== id) return false;
    this.prompt = undefined;
    this.dirty = true;
    this.bumpDesired();
    await this.ensureDrain();
    return this.prompt === undefined && this.renderedPromptId === undefined
      && this.lastPaintSucceeded;
  }

  /** Returns whether the logical viewport actually moved. The bridge write is
   * serialized behind any in-flight revision and can be observed with
   * whenIdle(). */
  scroll(direction: 'up' | 'down'): boolean {
    if (!this.canInteract() || this.prompt || (this.desired?.model.approvals ?? 0) > 0) return false;
    if (!this.viewport.scroll(direction)) return false;
    if (!this.desired) return false;
    this.bumpDesired();
    this.dirty = true;
    void this.ensureDrain();
    return true;
  }

  isActive(): boolean { return this.active; }

  exportState(): NativeTerminalSurfaceState {
    return { version: 1, viewport: this.viewport.exportState() };
  }

  importState(raw: unknown): boolean {
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return false;
    const value = raw as Record<string, unknown>;
    if (value.version !== 1) return false;
    if (this.active) this.clearVisit();
    return this.viewport.importState(value.viewport);
  }

  /** Reinstall the fixed native page after host UI may have discarded it. */
  async repaint(): Promise<boolean> {
    if (!this.canInteract() || !this.desired) return false;
    await this.whenIdle();
    if (!this.canInteract() || !this.desired) return false;
    const desired = this.desired;
    this.pageMounted = false;
    this.physicalBodyText = '';
    this.bumpDesired();
    this.dirty = true;
    await this.ensureDrain();
    return this.desired !== desired
      && this.pageMounted && this.lastPaintSucceeded;
  }

  async whenIdle(): Promise<void> {
    for (;;) {
      const work = this.draining;
      if (!work) return;
      await work;
      if (this.draining === work) return;
    }
  }

  /** Stop scheduling native writes and wait for the already-issued call before
   * a presenter replaces this page. */
  async pauseForHandoff(): Promise<boolean> {
    if (!this.active || this.paused) return false;
    this.paused = true;
    this.dirty = false;
    await this.whenIdle();
    return this.active && this.paused;
  }

  /** The replacement page won. Retain only viewport history, which is keyed by
   * conversation and restores the reading anchor when that conversation opens. */
  completeHandoff(): boolean {
    if (!this.active || !this.paused) return false;
    this.clearVisit();
    return true;
  }

  /** The replacement failed, so reinstall the known native page before
   * accepting more terminal interaction. */
  async resume(): Promise<boolean> {
    if (!this.active || !this.paused || !this.desired) return false;
    this.paused = false;
    this.pageMounted = false;
    this.physicalBodyText = '';
    this.bumpDesired();
    this.dirty = true;
    await this.ensureDrain();
    return this.active && !this.paused && this.pageMounted && this.lastPaintSucceeded;
  }

  async resumeAfterFailedHandoff(): Promise<boolean> { return this.resume(); }

  retire(): boolean {
    if (!this.active) return false;
    this.clearVisit();
    return true;
  }

  retireForHandoff(): boolean { return this.retire(); }

  private canInteract(): boolean {
    return this.active && !this.paused && this.desired !== undefined;
  }

  private canWrite(): boolean {
    return this.canInteract();
  }

  private conversationKey(model: BrainstormHudModel): string {
    if (isBrainstorm(model)) return `brainstorm:${model.topic.replace(/\s+/gu, ' ').trim()}`;
    return model.conversationId?.trim() || `agent:${focusName(model.topic)}`;
  }

  private bumpDesired(): void {
    if (!this.desired) return;
    this.desired = { ...this.desired, token: ++this.nextToken };
  }

  private pageFor(desired: DesiredTerminal, prompt: TerminalPrompt | undefined): NativeTerminalPage | undefined {
    if (!current(desired.isCurrent)) return;
    const question = promptRows(prompt, this.measureTextWidth);
    if (question === undefined) return;
    const contentRows = NATIVE_TERMINAL_GEOMETRY.bodyLineCapacity - BODY_FIXED_ROWS;
    const transcriptRows = Math.max(1, contentRows - question.length);
    const page = this.viewport.view(
      this.conversationKey(desired.model),
      messagesFor(desired.model),
      transcriptRows,
      (text) => wrapWithMetrics(text, this.measureTextWidth),
      desired.model.historyTruncated,
      `${NATIVE_TERMINAL_GEOMETRY.bodyTextWidth}|${MAX_LINE_UNITS}`,
    );
    const brainstorm = isBrainstorm(desired.model);
    const visible = page.lines.length > 0 ? page.lines
      : [brainstorm ? 'Say “JOAT, brainstorm …” or tap to speak.' : 'Tap to speak to this agent.'];
    const bodyRows = [...visible, ...question];
    while (bodyRows.length < contentRows) bodyRows.push(' ');
    // The page's head line IS the status header (clock · page · mic ·
    // battery, composed by the presenter), so the body's state row no
    // longer repeats it.
    const status = oneMeasuredLine(
      brainstorm
        ? brainstormStatusRow(desired.model)
        : `${focusName(desired.model.topic)} > ${stateName(desired.model)}`,
      this.measureTextWidth,
    );
    const footer = oneMeasuredLine(
      brainstorm && prompt === undefined
        ? brainstormFooter(desired.model, page.newer)
        : nativeTerminalFooter(desired.model, page.older, page.newer, prompt !== undefined),
      this.measureTextWidth,
    );
    const bodyText = [status, ...bodyRows.slice(0, contentRows), footer].join('\n');
    if (bodyText.length > MAX_UPGRADE_UNITS) return;
    return {
      headText: desired.status ?? headTextFor(desired.model),
      bodyText,
      conversationKey: this.conversationKey(desired.model),
      older: page.older,
      newer: page.newer,
      state: desired.model.state,
      ...(prompt ? { promptId: prompt.id } : {}),
    };
  }

  private ensureDrain(): Promise<void> {
    if (this.draining) return this.draining;
    const work = this.drain();
    const tracked = work.finally(() => {
      if (this.draining !== tracked) return;
      this.draining = undefined;
      if (this.dirty && this.canWrite()) void this.ensureDrain();
    });
    this.draining = tracked;
    return tracked;
  }

  private async drain(): Promise<void> {
    while (this.canWrite() && this.dirty) {
      this.dirty = false;
      const desired = this.desired;
      if (!desired) return;
      const prompt = this.prompt && current(this.prompt.isCurrent) ? this.prompt : undefined;
      if (this.prompt && !prompt) this.prompt = undefined;
      const page = this.pageFor(desired, prompt);
      if (!page) {
        this.lastPaintSucceeded = false;
        continue;
      }
      const snapshot: RenderSnapshot = { desired, prompt, page };
      let painted = this.pageMounted
        ? await this.upgradeBody(snapshot)
        : await this.rebuild(snapshot);
      // A false native-text receipt commonly means the host discarded this
      // page during a WebView or plugin transition. Reinstall the same fixed
      // native document once; never fall back to the four-image page.
      if (!painted && this.pageMounted && this.snapshotCurrent(snapshot)) {
        this.pageMounted = false;
        this.physicalBodyText = '';
        painted = await this.rebuild(snapshot);
      }
      if (!this.snapshotCurrent(snapshot)) continue;
      this.lastPaintSucceeded = painted;
      if (!painted) continue;
      this.pageMounted = true;
      this.renderedToken = desired.token;
      this.renderedPromptId = prompt?.id;
    }
  }

  private snapshotCurrent(snapshot: RenderSnapshot): boolean {
    return this.canWrite()
      && this.desired === snapshot.desired
      && this.desired.token === snapshot.desired.token
      && this.prompt === snapshot.prompt
      && current(snapshot.desired.isCurrent)
      && (snapshot.prompt === undefined || current(snapshot.prompt.isCurrent));
  }

  private async rebuild(snapshot: RenderSnapshot): Promise<boolean> {
    if (!this.snapshotCurrent(snapshot)) return false;
    let layout: RebuildPageContainer;
    try { layout = this.layout(snapshot.page); } catch { return false; }
    if (!validLayout(layout, snapshot.page)) return false;
    try {
      const replaced = await this.replacePage(layout);
      if (replaced && this.active && this.desired?.visit === snapshot.desired.visit) {
        this.pageMounted = true;
        this.physicalBodyText = snapshot.page.bodyText;
        this.physicalHeadText = snapshot.page.headText;
      }
      return replaced;
    } catch { return false; }
  }

  private async upgradeBody(snapshot: RenderSnapshot): Promise<boolean> {
    if (!this.snapshotCurrent(snapshot)) return false;
    if (!await this.upgradeHead(snapshot)) return false;
    if (!this.snapshotCurrent(snapshot)) return false;
    const previous = this.physicalBodyText;
    if (previous === snapshot.page.bodyText) return true;
    try {
      const accepted = await this.client.require().textContainerUpgrade(
        new TextContainerUpgrade({
          containerID: NativeTerminalContainer.Body.id,
          containerName: NativeTerminalContainer.Body.name,
          contentOffset: 0,
          contentLength: Math.max(previous.length, snapshot.page.bodyText.length),
          content: snapshot.page.bodyText || ' ',
        }),
      );
      if (accepted && this.active && this.desired?.visit === snapshot.desired.visit) {
        this.physicalBodyText = snapshot.page.bodyText;
      }
      return accepted;
    } catch { return false; }
  }

  private async upgradeHead(snapshot: RenderSnapshot): Promise<boolean> {
    const previous = this.physicalHeadText;
    if (previous === snapshot.page.headText) return true;
    try {
      const accepted = await this.client.require().textContainerUpgrade(
        new TextContainerUpgrade({
          containerID: NativeTerminalContainer.Head.id,
          containerName: NativeTerminalContainer.Head.name,
          contentOffset: 0,
          contentLength: Math.max(previous.length, snapshot.page.headText.length),
          content: snapshot.page.headText || ' ',
        }),
      );
      if (accepted && this.active && this.desired?.visit === snapshot.desired.visit) {
        this.physicalHeadText = snapshot.page.headText;
      }
      return accepted;
    } catch { return false; }
  }

  private clearVisit(): void {
    this.visit += 1;
    this.nextToken += 1;
    this.active = false;
    this.paused = false;
    this.pageMounted = false;
    this.dirty = false;
    this.desired = undefined;
    this.prompt = undefined;
    this.physicalBodyText = '';
    this.physicalHeadText = '';
    this.lastPaintSucceeded = false;
    this.renderedPromptId = undefined;
  }
}
