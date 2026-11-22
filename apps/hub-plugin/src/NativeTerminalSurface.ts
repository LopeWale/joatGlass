import {
  TextContainerUpgrade,
  utf8ByteLength,
  type RebuildPageContainer,
} from '@evenrealities/even_hub_sdk';
import { getTextWidth, pxTruncate } from '@evenrealities/pretext';
import type { BridgeClient } from './BridgeClient';
import { TEXT_CONTAINER_MAX_BYTES, fitUtf8 } from './FirmwareLimits';
import { firmwareLineWidth } from './FirmwareText';
import {
  TerminalConversationViewport,
  type TerminalLayeredLine,
} from './TerminalConversation';
import { wrapTerminalParagraph } from './TerminalText';
import {
  ContainerId,
  type AgentTerminalHudModel,
  type TerminalConversationMessage,
  type TerminalHudState,
} from './types';

/** Page-local container identities for the 5-container framed Agent Terminal.
 *
 * - Head (`StatusBar = 1`): live status bar (or swapped for image spine 16/17).
 * - Card (`12`): rounded card frame (`borderWidth: 1, borderColor: 8, borderRadius: 4`) + sole event capture owner.
 * - Dim (`13`): 7-row dim layer (`textColor: 2`) for `> ` user turns, `>> ` tool calls, `! ` notices, and dimmed context above approvals.
 * - Bright (`14`): 7-row bright layer (`textColor: 4`) for full-width assistant prose, live `> ` speech, and the Unicode approval box.
 * - Foot (`Hint = 7`): 1-line dim footer (`textColor: 2`) for state + truthful gestures.
 */
export const NativeTerminalContainer = {
  Head: { id: ContainerId.StatusBar, name: 'terminal-head' },
  Card: { id: 12, name: 'terminal-card' },
  Dim: { id: 13, name: 'terminal-dim' },
  Bright: { id: 14, name: 'terminal-bright' },
  Foot: { id: ContainerId.Hint, name: 'terminal-foot' },
} as const;

export const NATIVE_TERMINAL_GEOMETRY = {
  width: 576,
  headX: 0,
  headY: 0,
  headWidth: 576,
  headHeight: 32,
  headPadding: 2,
  headTextWidth: 572,
  cardX: 12,
  cardY: 34,
  cardWidth: 552,
  cardHeight: 214,
  cardBorderWidth: 1,
  cardBorderColor: 8,
  cardBorderRadius: 4,
  bodyX: 24,
  bodyY: 42,
  bodyWidth: 528,
  bodyHeight: 198,
  bodyTextWidth: 528,
  bodyLineCapacity: 7,
  lineHeight: 27,
  footX: 12,
  footY: 252,
  footWidth: 552,
  footHeight: 34,
  footTextWidth: 552,
} as const;

export const TERMINAL_WORKING_SPINNERS = ['◐', '◓', '◑', '◒'] as const;

const MAX_PROMPT_ROWS = 4;
const MAX_LINE_UNITS = 190;
const USER_PREFIX = '> ';
const USER_INDENT = '  ';
const NOTICE_PREFIX = '! ';
const NOTICE_INDENT = '  ';

const MODEL_ID_PATTERN = /\b(?:gemini-[\w.-]+|claude-(?:sonnet|opus|haiku|3|4)[\w.-]*|sonnet-[\w.-]+|opus-[\w.-]+|haiku-[\w.-]+|gpt-[\w.-]+|o[134]-[\w.-]+)\b/giu;

export interface NativeTerminalPage {
  headText: string;
  dimLines: readonly string[];
  brightLines: readonly string[];
  dimText: string;
  brightText: string;
  footText: string;
  conversationKey: string;
  older: number;
  newer: number;
  state: TerminalHudState;
  promptId?: string;
}

export type NativeTerminalLayoutFactory = (
  page: Readonly<NativeTerminalPage>,
) => RebuildPageContainer;

export interface NativeTerminalSurfaceOptions {
  measureTextWidth?: (text: string) => number;
  /** Whether the mounted page's status line is the image spine. The
   * presenter repaints that from the status model, so the head text
   * container is not on the glass to upgrade. */
  imageHeader?: () => boolean;
}

export interface NativeTerminalSurfaceState {
  version: 1;
  viewport: ReturnType<TerminalConversationViewport['exportState']>;
}

interface TerminalPrompt {
  id: string;
  question: string;
  isCurrent: () => boolean;
  partial?: string;
  transcribing?: boolean;
}

interface DesiredTerminal {
  visit: number;
  token: number;
  model: AgentTerminalHudModel;
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

function copyModel(model: AgentTerminalHudModel): AgentTerminalHudModel {
  return {
    ...model,
    lanes: [...model.lanes],
    messages: model.messages?.map((message) => ({ ...message })),
  };
}

/** Remove raw model IDs from HUD strings so model names never clutter 576x288 lenses. */
export function stripTerminalModelIds(value: string): string {
  return value
    .replace(/\(\s*(?:gemini|claude|gpt|sonnet|opus|haiku|o[134])[^)]*\)/giu, '')
    .replace(MODEL_ID_PATTERN, '')
    .replace(/\s*·\s*·\s*/gu, ' · ')
    .replace(/\s+/gu, ' ')
    .trim();
}

/** Clean title-cased agent display name from `model.focus` (never a model ID). */
export function terminalAgentTitle(focus: string): string {
  const head = (focus.split(' · ')[0] ?? '')
    .replace(/\([^)]*\)/gu, ' ')
    .replace(MODEL_ID_PATTERN, ' ')
    .replace(/\s+/gu, ' ')
    .trim();
  const lower = head.toLowerCase();
  if (lower.startsWith('claude')) return 'Claude';
  if (lower.startsWith('codex')) return 'Codex';
  if (lower.startsWith('antigravity')) return 'Antigravity';
  if (lower.startsWith('openclaw')) return 'OpenClaw';
  if (lower.startsWith('hermes')) return 'Hermes';
  if (!head) return 'Agent';
  return head.charAt(0).toUpperCase() + head.slice(1);
}

/** Uppercase header token for `terminal-head` (`3:45 · CLAUDE · ● · ▇`). */
export function terminalHeadTitle(focus: string): string {
  const title = terminalAgentTitle(focus);
  return title === 'Agent' ? 'AGENT TERMINAL' : title.toUpperCase();
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

function combinedMeasure(custom: (text: string) => number): (text: string) => number {
  if (custom === firmwareLineWidth) {
    return (text: string) => Math.max(firmwareLineWidth(text), getTextWidth(text));
  }
  return (text: string) => Math.max(custom(text), firmwareLineWidth(text));
}

function wrapToWidth(
  text: string,
  maxWidth: number,
  measureTextWidth: (text: string) => number,
): string[] {
  const measure = combinedMeasure(measureTextWidth);
  return wrapTerminalParagraph(
    text,
    maxWidth,
    (candidate) => (candidate.length > MAX_LINE_UNITS ? maxWidth + 1 : measure(candidate)),
  ).map((line) => line.replace(/\s+$/u, '') || ' ');
}

export function oneMeasuredTerminalLine(
  text: string,
  maxWidth: number,
  measureTextWidth: (text: string) => number = firmwareLineWidth,
): string {
  const measure = combinedMeasure(measureTextWidth);
  const compact = Array.from(text.replace(/\s+/gu, ' ').trim()).slice(0, MAX_LINE_UNITS).join('');
  if (!compact) return ' ';
  if (measure(compact) <= maxWidth) return compact;
  let candidate = pxTruncate(compact, maxWidth);
  if (measure(candidate) <= maxWidth) return candidate;
  const points = Array.from(compact);
  while (points.length > 1) {
    points.pop();
    candidate = `${points.join('').trimEnd()}…`;
    if (measure(candidate) <= maxWidth) return candidate;
  }
  return '…';
}

function wrapPrefixedBlock(
  text: string,
  firstPrefix: string,
  hangingPrefix: string,
  maxWidth: number,
  measureTextWidth: (text: string) => number,
): string[] {
  const measure = combinedMeasure(measureTextWidth);
  const prefixWidth = Math.max(measure(firstPrefix), measure(hangingPrefix));
  const contentWidth = Math.max(48, maxWidth - prefixWidth);
  const paragraphs = text.replace(/\r\n?/gu, '\n').split('\n');
  const out: string[] = [];
  let isFirst = true;
  for (const raw of paragraphs) {
    const clean = raw.replace(/\s+/gu, ' ').trim();
    if (!clean) continue;
    const wrapped = wrapToWidth(clean, contentWidth, measureTextWidth);
    for (const segment of wrapped) {
      const prefix = isFirst ? firstPrefix : hangingPrefix;
      isFirst = false;
      const candidate = `${prefix}${segment.trim()}`;
      out.push(measure(candidate) <= maxWidth
        ? candidate
        : oneMeasuredTerminalLine(candidate, maxWidth, measureTextWidth));
    }
  }
  return out;
}

/** Parse a `⏺ ` tool line from Claude Code, Codex, or Antigravity into a compact
 * 1-line `>> <tool> · <detail>` row measured against `maxWidth`. */
export function formatToolLine(
  raw: string,
  maxWidth: number = NATIVE_TERMINAL_GEOMETRY.bodyTextWidth,
  measureTextWidth: (text: string) => number = firmwareLineWidth,
): string {
  const body = raw.replace(/^\s*⏺\s*/u, '').replace(/\s+/gu, ' ').trim();
  if (!body) return '>> tool';
  let formatted: string;
  const callMatch = /^([A-Za-z0-9_./-]+)\((.*)\)$/u.exec(body);
  if (callMatch) {
    const tool = callMatch[1]!.trim();
    const detail = callMatch[2]!.trim();
    formatted = detail ? `>> ${tool} · ${detail}` : `>> ${tool}`;
  } else {
    const colonIndex = body.indexOf(':');
    if (colonIndex > 0 && !body.slice(0, colonIndex).includes(' ')) {
      const tool = body.slice(0, colonIndex).trim();
      const detail = body.slice(colonIndex + 1).trim();
      formatted = detail ? `>> ${tool} · ${detail}` : `>> ${tool}`;
    } else {
      formatted = `>> ${body}`;
    }
  }
  return oneMeasuredTerminalLine(formatted, maxWidth, measureTextWidth);
}

function isApprovalLine(line: string): boolean {
  return /^\s*\[approval\]/iu.test(line);
}

function parseApprovalSummaryFromText(raw: string): string | undefined {
  const match = /^\s*\[approval\]\s*(.+?)(?:\s*[—-]\s*say\s+['"“]?terminal\s+approve.*)?$/iu.exec(raw.trim());
  if (!match) return undefined;
  const body = match[1]!.replace(/\s+/gu, ' ').trim();
  if (!body) return undefined;
  const colonIndex = body.indexOf(':');
  if (colonIndex > 0 && !body.slice(0, colonIndex).includes(' ')) {
    const kind = body.slice(0, colonIndex).trim();
    const detail = body.slice(colonIndex + 1).trim();
    return detail ? `${kind} · ${detail}` : kind;
  }
  return body;
}

function approvalSummaryFor(model: AgentTerminalHudModel): string {
  const messages = model.messages ?? [];
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]!;
    const lines = message.text.replace(/\r\n?/gu, '\n').split('\n');
    for (let lineIdx = lines.length - 1; lineIdx >= 0; lineIdx -= 1) {
      const parsed = parseApprovalSummaryFromText(lines[lineIdx]!);
      if (parsed) return parsed;
    }
  }
  const activityLines = model.activity.replace(/\r\n?/gu, '\n').split('\n');
  for (let index = activityLines.length - 1; index >= 0; index -= 1) {
    const parsed = parseApprovalSummaryFromText(activityLines[index]!);
    if (parsed) return parsed;
  }
  const fallback = stripTerminalModelIds(model.terminalReason ?? '')
    || stripTerminalModelIds(model.activity);
  return fallback && !/^ready$/iu.test(fallback) ? fallback : 'Tool execution requested';
}

/** Build a firmware-measured Unicode approval box (`<= maxWidth` on every row). */
export function buildApprovalBox(
  summary: string,
  count = 1,
  maxWidth: number = NATIVE_TERMINAL_GEOMETRY.bodyTextWidth,
  measureTextWidth: (text: string) => number = firmwareLineWidth,
): string[] {
  const measure = combinedMeasure(measureTextWidth);
  const title = count > 1 ? `Approve command (${count})` : 'Approve command';

  let topDashes = 2;
  let top = `╭── ${title} ──╮`;
  if (measure(top) > maxWidth) {
    top = oneMeasuredTerminalLine(`╭─ ${title} ─╮`, maxWidth, measureTextWidth);
  } else {
    while (topDashes < 64) {
      const candidate = `╭── ${title} ${'─'.repeat(topDashes + 1)}╮`;
      if (measure(candidate) > maxWidth) break;
      topDashes += 1;
      top = candidate;
    }
  }
  const targetWidth = Math.min(maxWidth, measure(top));

  let bottomDashes = 2;
  let bottom = '╰──╯';
  while (bottomDashes < 80) {
    const candidate = `╰${'─'.repeat(bottomDashes + 1)}╯`;
    if (measure(candidate) > targetWidth || measure(candidate) > maxWidth) break;
    bottomDashes += 1;
    bottom = candidate;
  }

  const barPrefix = '│ ';
  const innerWidth = Math.max(48, maxWidth - measure(barPrefix));
  const cleanSummary = summary.replace(/\s+/gu, ' ').trim() || 'Tool execution requested';
  const wrapped = wrapToWidth(cleanSummary, innerWidth, measureTextWidth);
  const contentRows: string[] = [];
  if (wrapped.length <= 2) {
    for (const row of wrapped) {
      contentRows.push(oneMeasuredTerminalLine(`${barPrefix}${row}`, maxWidth, measureTextWidth));
    }
  } else {
    contentRows.push(oneMeasuredTerminalLine(`${barPrefix}${wrapped[0]!}`, maxWidth, measureTextWidth));
    const rest = wrapped.slice(1).join(' ');
    contentRows.push(oneMeasuredTerminalLine(`${barPrefix}${rest}`, maxWidth, measureTextWidth));
  }

  return [top, ...contentRows, bottom];
}

/** Strip markdown link targets and U+0060 backticks (missing from G2 LVGL font). */
export function cleanTerminalMarkdown(value: string): string {
  return value
    .replace(/\[([^\]\n]+)\]\((?:https?:\/\/|file:\/\/)[^)\s]*\)/giu, '$1')
    .replace(/`+/gu, '');
}

function formatTerminalMessage(
  message: TerminalConversationMessage,
  _hideApprovals: boolean,
  maxWidth: number,
  measureTextWidth: (text: string) => number,
): TerminalLayeredLine[] {
  if (message.role === 'user') {
    const cleaned = cleanTerminalMarkdown(message.text.replace(/^\s*(?:YOU\s*>\s*|>\s*)/iu, ''));
    return wrapPrefixedBlock(cleaned, USER_PREFIX, USER_INDENT, maxWidth, measureTextWidth)
      .map((text) => ({ layer: 'dim', text }));
  }

  if (message.role === 'status') {
    const out: TerminalLayeredLine[] = [];
    for (const rawLine of message.text.replace(/\r\n?/gu, '\n').split('\n')) {
      const trimmed = rawLine.trim();
      if (!trimmed) continue;
      if (isApprovalLine(trimmed)) continue;
      if (trimmed.startsWith('⏺')) {
        out.push({ layer: 'dim', text: formatToolLine(cleanTerminalMarkdown(trimmed), maxWidth, measureTextWidth) });
        continue;
      }
      const normalized = stripTerminalModelIds(cleanTerminalMarkdown(
        trimmed
          .replace(/^\[!\]\s*/u, '')
          .replace(/^!\s*/u, '')
          .replace(/^\[resumed\]\s*/iu, 'Resumed ')
          .replace(/\s*[—-]\s*say\s+['"“]?terminal\s+approve.*$/iu, ''),
      ));
      if (!normalized) continue;
      for (const line of wrapPrefixedBlock(normalized, NOTICE_PREFIX, NOTICE_INDENT, maxWidth, measureTextWidth)) {
        out.push({ layer: 'dim', text: line });
      }
    }
    return out;
  }

  // Assistant turn: prose on Bright (full width, NO `AGENT >` prefix), `⏺` tool calls on Dim.
  const out: TerminalLayeredLine[] = [];
  const rawLines = message.text
    .replace(/^\s*AGENT\s*>\s*/iu, '')
    .replace(/\r\n?/gu, '\n')
    .split('\n');
  for (const rawLine of rawLines) {
    const trimmed = rawLine.trim();
    if (!trimmed) {
      if (out.length > 0 && out[out.length - 1]?.layer !== 'blank') {
        out.push({ layer: 'blank', text: ' ' });
      }
      continue;
    }
    if (isApprovalLine(trimmed)) continue;
    if (trimmed.startsWith('⏺')) {
      if (out[out.length - 1]?.layer === 'blank') out.pop();
      out.push({ layer: 'dim', text: formatToolLine(cleanTerminalMarkdown(trimmed), maxWidth, measureTextWidth) });
      continue;
    }
    const cleanLine = cleanTerminalMarkdown(trimmed);
    if (!cleanLine.trim()) continue;
    for (const wrapped of wrapToWidth(cleanLine, maxWidth, measureTextWidth)) {
      out.push({ layer: 'bright', text: wrapped });
    }
  }
  while (out[out.length - 1]?.layer === 'blank') out.pop();
  return out;
}

function messagesFor(model: AgentTerminalHudModel): readonly TerminalConversationMessage[] {
  if (model.messages !== undefined && model.messages.length > 0) {
    return model.messages;
  }
  if (model.state === 'ready' || model.state === 'settled') {
    const activity = stripTerminalModelIds(model.activity);
    if (model.messages !== undefined || !activity || /^ready\b/iu.test(activity)) {
      return [];
    }
  }
  const fallback = stripTerminalModelIds(model.terminalReason ?? '')
    || stripTerminalModelIds(model.activity);
  if (!fallback || /^ready$/iu.test(fallback)) return [];
  return [{ id: '__activity', role: 'status', text: fallback }];
}

function promptBrightRows(
  prompt: TerminalPrompt | undefined,
  maxWidth: number,
  measureTextWidth: (text: string) => number,
): string[] | undefined {
  if (!prompt) return [];
  if (!validQuestion(prompt.id, prompt.question) || !current(prompt.isCurrent)) return undefined;
  const partial = prompt.partial?.replace(/\s+/gu, ' ').trim();
  if (!partial) {
    return [prompt.transcribing ? '> …' : '> Listening…'];
  }
  const wrapped = wrapPrefixedBlock(partial, USER_PREFIX, USER_INDENT, maxWidth, measureTextWidth);
  if (wrapped.length <= MAX_PROMPT_ROWS) return wrapped;
  return ['> …', ...wrapped.slice(-(MAX_PROMPT_ROWS - 1))];
}

/** Truthful 1-line footer for `terminal-foot` (`12, 252, 552, 34`, `textColor: 2`). */
export function nativeTerminalFooter(
  model: AgentTerminalHudModel,
  older: number,
  newer: number,
  options: {
    promptVisible?: boolean;
    promptTranscribing?: boolean;
    elapsedSeconds?: number;
    spinnerIndex?: number;
    measureTextWidth?: (text: string) => number;
  } = {},
): string {
  const measure = options.measureTextWidth ?? firmwareLineWidth;
  const maxWidth = NATIVE_TERMINAL_GEOMETRY.footTextWidth;

  if (options.promptVisible) {
    return oneMeasuredTerminalLine(
      options.promptTranscribing
        ? '◐ Transcribing… · 2x: cancel'
        : '● Listening · tap: done · 2x: cancel',
      maxWidth,
      measure,
    );
  }

  if ((model.approvals ?? 0) > 0) {
    return oneMeasuredTerminalLine(
      'tap: approve · swipe: deny · 2x: sessions',
      maxWidth,
      measure,
    );
  }

  if (model.state === 'running') {
    const spinner = TERMINAL_WORKING_SPINNERS[
      Math.abs(options.spinnerIndex ?? 2) % TERMINAL_WORKING_SPINNERS.length
    ]!;
    const elapsed = options.elapsedSeconds !== undefined && options.elapsedSeconds >= 1
      ? ` · ${options.elapsedSeconds}s`
      : '';
    const scrollCue = newer > 0 ? ` · ▼ ${newer}` : (older > 0 ? ' · swipe: scroll' : '');
    const candidate = `${spinner} Working${elapsed}${scrollCue} · hold: stop · 2x: sessions`;
    if (combinedMeasure(measure)(candidate) <= maxWidth) return candidate;
    return oneMeasuredTerminalLine(
      `${spinner} Working${elapsed} · hold: stop · 2x: sessions`,
      maxWidth,
      measure,
    );
  }

  const primary = model.state === 'blocked'
    || model.state === 'unavailable'
    || model.state === 'approval_required'
    ? 'tap: retry'
    : 'tap: speak';

  const parts = [
    ...(newer > 0 ? [`▼ ${newer} newer`] : []),
    primary,
    ...(older > 0 || newer > 0 ? ['swipe: scroll'] : []),
    '2x: sessions',
  ];
  return oneMeasuredTerminalLine(parts.join(' · '), maxWidth, measure);
}

function validLayout(layout: RebuildPageContainer, page: NativeTerminalPage): boolean {
  const text = layout.textObject ?? [];
  const head = text.find((item) => item.containerID === NativeTerminalContainer.Head.id
    && item.containerName === NativeTerminalContainer.Head.name);
  const card = text.find((item) => item.containerID === NativeTerminalContainer.Card.id
    && item.containerName === NativeTerminalContainer.Card.name);
  const dim = text.find((item) => item.containerID === NativeTerminalContainer.Dim.id
    && item.containerName === NativeTerminalContainer.Dim.name);
  const bright = text.find((item) => item.containerID === NativeTerminalContainer.Bright.id
    && item.containerName === NativeTerminalContainer.Bright.name);
  const foot = text.find((item) => item.containerID === NativeTerminalContainer.Foot.id
    && item.containerName === NativeTerminalContainer.Foot.name);
  return layout.containerTotalNum === 5
    && text.length === 5
    && (layout.imageObject?.length ?? 0) === 0
    && (layout.listObject?.length ?? 0) === 0
    && text.filter((item) => item.isEventCapture === 1).length === 1
    && head?.isEventCapture === 0
    && card?.isEventCapture === 1
    && dim?.isEventCapture === 0
    && bright?.isEventCapture === 0
    && foot?.isEventCapture === 0
    && card.content === ' '
    && head.content === (page.headText || ' ')
    && dim.content === (page.dimText || ' ')
    && bright.content === (page.brightText || ' ')
    && foot.content === (page.footText || ' ')
    && [head, card, dim, bright, foot].every(
      (item) => item !== undefined && utf8ByteLength(item.content ?? '') <= TEXT_CONTAINER_MAX_BYTES,
    );
}

/**
 * Owns the 5-container framed dual-brightness Agent Terminal surface.
 * Installs the layout once per visit via `RebuildPageContainer` and uses
 * selective `TextContainerUpgrade` writes on `Head`, `Dim`, `Bright`, and `Foot`
 * for all streaming, speech-in-text, scrolling, spinner ticks, and approvals.
 */
export class NativeTerminalSurface {
  private readonly viewport = new TerminalConversationViewport();
  private readonly measureTextWidth: (text: string) => number;
  private readonly imageHeader: () => boolean;
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
  private committedPage?: NativeTerminalPage;
  private physicalHeadText = '';
  private physicalDimText = '';
  private physicalBrightText = '';
  private physicalFootText = '';
  private thinkingSince?: number;
  private spinnerIndex = 2; // starts at '◑' on initial working frame, cycles ['◐', '◓', '◑', '◒'] on ticks
  private lastPaintSucceeded = false;
  private draining?: Promise<void>;

  constructor(
    private readonly client: Pick<BridgeClient, 'require'>,
    private readonly layout: NativeTerminalLayoutFactory,
    private readonly replacePage: (page: RebuildPageContainer) => Promise<boolean>,
    options: NativeTerminalSurfaceOptions = {},
  ) {
    this.measureTextWidth = options.measureTextWidth ?? firmwareLineWidth;
    this.imageHeader = options.imageHeader ?? (() => false);
  }

  async present(
    model: AgentTerminalHudModel,
    status?: string,
    isCurrent: () => boolean = () => true,
  ): Promise<boolean> {
    if (!current(isCurrent) || this.paused) return false;
    const copy = copyModel(model);
    const key = this.conversationKey(copy);
    const entering = !this.active || this.desired === undefined;
    const conversationChanged = !entering
      && this.desired !== undefined
      && this.conversationKey(this.desired.model) !== key;
    if (entering) this.visit += 1;
    const retainedStatus = !entering ? this.desired?.status : undefined;
    const nextStatus = status?.trim()
      ? stripTerminalModelIds(status)
      : retainedStatus;
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
    if (entering || conversationChanged) {
      this.thinkingSince = undefined;
      this.spinnerIndex = 2;
    }
    const terminalWorking = copy.state === 'running' && (copy.approvals ?? 0) === 0;
    if (terminalWorking) {
      if (this.thinkingSince === undefined) {
        this.thinkingSince = Date.now();
        this.spinnerIndex = 2;
      }
    } else {
      this.thinkingSince = undefined;
      this.spinnerIndex = 2;
    }
    if (entering) {
      this.pageMounted = false;
      this.physicalHeadText = '';
      this.physicalDimText = '';
      this.physicalBrightText = '';
      this.physicalFootText = '';
      this.renderedPromptId = undefined;
      this.prompt = undefined;
    } else if (conversationChanged) {
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
    if (promptBrightRows(candidate, NATIVE_TERMINAL_GEOMETRY.bodyTextWidth, this.measureTextWidth) === undefined) {
      return false;
    }
    const existing = this.prompt;
    if (existing?.id === id) {
      return existing.question === question && this.renderedPromptId === id
        && this.lastPaintSucceeded && current(existing.isCurrent);
    }
    this.prompt = candidate;
    this.dirty = true;
    this.bumpDesired();
    await this.ensureDrain();
    return this.prompt?.id === id && this.renderedPromptId === id
      && this.lastPaintSucceeded && current(isCurrent);
  }

  async showPromptPartial(id: string, text: string): Promise<boolean> {
    const existing = this.prompt;
    if (!this.canInteract() || existing?.id !== id || !current(existing.isCurrent)) return false;
    if (text.length > 600 || text.includes('\0')) return false;
    const clean = text.replace(/\s+/gu, ' ').trim();
    const nextPartial = clean.length > 0 ? clean : undefined;
    const nextTranscribing = clean.length > 0 ? existing.transcribing : false;
    if (existing.partial === nextPartial && existing.transcribing === nextTranscribing) {
      return this.renderedPromptId === id && this.lastPaintSucceeded && current(existing.isCurrent);
    }
    const updated: TerminalPrompt = {
      id: existing.id,
      question: existing.question,
      isCurrent: existing.isCurrent,
      ...(nextPartial !== undefined ? { partial: nextPartial } : {}),
      ...(nextTranscribing ? { transcribing: true } : {}),
    };
    this.prompt = updated;
    this.dirty = true;
    this.bumpDesired();
    await this.ensureDrain();
    return this.prompt?.id === id && this.prompt.partial === nextPartial
      && this.renderedPromptId === id && this.lastPaintSucceeded && current(updated.isCurrent);
  }

  async showPromptTranscribing(id: string): Promise<boolean> {
    const existing = this.prompt;
    if (!this.canInteract() || existing?.id !== id || !current(existing.isCurrent)) return false;
    if (existing.transcribing === true) {
      return this.renderedPromptId === id && this.lastPaintSucceeded && current(existing.isCurrent);
    }
    const updated: TerminalPrompt = {
      ...existing,
      transcribing: true,
    };
    this.prompt = updated;
    this.dirty = true;
    this.bumpDesired();
    await this.ensureDrain();
    return this.prompt?.id === id && this.prompt.transcribing === true
      && this.renderedPromptId === id && this.lastPaintSucceeded && current(updated.isCurrent);
  }

  async setStatus(status: string): Promise<boolean> {
    if (!this.canInteract() || !this.desired) return false;
    const clean = stripTerminalModelIds(status);
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

  renderedPage(): Readonly<NativeTerminalPage> | undefined {
    return this.committedPage;
  }

  isThinking(): boolean {
    return this.canInteract() && this.thinkingSince !== undefined && this.prompt === undefined;
  }

  /** Advance the local working spinner and elapsed counter via `terminal-foot` upgrade only. */
  tick(): boolean {
    if (!this.isThinking()) return false;
    this.spinnerIndex = (this.spinnerIndex + 1) % TERMINAL_WORKING_SPINNERS.length;
    this.bumpDesired();
    this.dirty = true;
    void this.ensureDrain();
    return true;
  }

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

  async repaint(): Promise<boolean> {
    if (!this.canInteract() || !this.desired) return false;
    await this.whenIdle();
    if (!this.canInteract() || !this.desired) return false;
    const desired = this.desired;
    this.pageMounted = false;
    this.physicalHeadText = '';
    this.physicalDimText = '';
    this.physicalBrightText = '';
    this.physicalFootText = '';
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

  async pauseForHandoff(): Promise<boolean> {
    if (!this.active || this.paused) return false;
    this.paused = true;
    this.dirty = false;
    await this.whenIdle();
    return this.active && this.paused;
  }

  completeHandoff(): boolean {
    if (!this.active || !this.paused) return false;
    this.clearVisit();
    return true;
  }

  async resume(): Promise<boolean> {
    if (!this.active || !this.paused || !this.desired) return false;
    this.paused = false;
    this.pageMounted = false;
    this.physicalHeadText = '';
    this.physicalDimText = '';
    this.physicalBrightText = '';
    this.physicalFootText = '';
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

  private conversationKey(model: AgentTerminalHudModel): string {
    return model.conversationId?.trim() || `agent:${terminalHeadTitle(model.focus)}`;
  }

  private bumpDesired(): void {
    if (!this.desired) return;
    this.desired = { ...this.desired, token: ++this.nextToken };
  }

  private pageFor(desired: DesiredTerminal, prompt: TerminalPrompt | undefined): NativeTerminalPage | undefined {
    if (!current(desired.isCurrent)) return undefined;
    const capacity = NATIVE_TERMINAL_GEOMETRY.bodyLineCapacity; // 7
    const width = NATIVE_TERMINAL_GEOMETRY.bodyTextWidth; // 528
    const promptLines = promptBrightRows(prompt, width, this.measureTextWidth);
    if (promptLines === undefined) return undefined;

    const approvals = prompt === undefined ? (desired.model.approvals ?? 0) : 0;
    const approvalBox = approvals > 0
      ? buildApprovalBox(approvalSummaryFor(desired.model), approvals, width, this.measureTextWidth)
      : [];

    const reservedBottom = promptLines.length > 0 ? promptLines.length : approvalBox.length;
    const transcriptCapacity = Math.max(1, capacity - reservedBottom);
    const hideApprovals = approvals > 0;
    const view = this.viewport.viewLayered(
      this.conversationKey(desired.model),
      messagesFor(desired.model),
      transcriptCapacity,
      (message) => formatTerminalMessage(message, hideApprovals, width, this.measureTextWidth),
      desired.model.historyTruncated,
      `${width}|${MAX_LINE_UNITS}|${hideApprovals ? 1 : 0}`,
    );

    const agentTitle = terminalAgentTitle(desired.model.focus);
    const visibleRows: TerminalLayeredLine[] = view.rows.length > 0
      ? view.rows.slice(0, transcriptCapacity)
      : (promptLines.length === 0 && approvalBox.length === 0)
        ? [{ layer: 'dim', text: oneMeasuredTerminalLine(`Tap to speak to ${agentTitle}.`, width, this.measureTextWidth) }]
        : [];

    const combined: TerminalLayeredLine[] = [];
    if (approvalBox.length > 0) {
      // Dim context rows above the bright Unicode approval callout box.
      const contextSlots = capacity - approvalBox.length;
      const contextRows = visibleRows.slice(-contextSlots);
      for (const row of contextRows) {
        combined.push(row.layer === 'blank' ? { layer: 'blank', text: ' ' } : { layer: 'dim', text: row.text });
      }
      while (combined.length < contextSlots) {
        combined.push({ layer: 'blank', text: ' ' });
      }
      for (const boxLine of approvalBox) {
        combined.push({ layer: 'bright', text: boxLine });
      }
    } else if (promptLines.length > 0) {
      const contextSlots = capacity - promptLines.length;
      let contextRows = visibleRows.slice(-contextSlots);
      if (contextRows[contextRows.length - 1]?.layer === 'bright' && contextSlots >= 2) {
        if (contextRows.length >= contextSlots) {
          contextRows = visibleRows.slice(-(contextSlots - 1));
        }
        for (const row of contextRows) combined.push(row);
        combined.push({ layer: 'blank', text: ' ' });
      } else {
        for (const row of contextRows) combined.push(row);
      }
      for (const line of promptLines) {
        combined.push({ layer: 'bright', text: line });
      }
      while (combined.length < capacity) {
        combined.push({ layer: 'blank', text: ' ' });
      }
    } else {
      for (const row of visibleRows) {
        combined.push(row);
      }
      while (combined.length < capacity) {
        combined.push({ layer: 'blank', text: ' ' });
      }
    }

    const aligned = combined.slice(0, capacity);
    const dimLines = aligned.map((row) => (row.layer === 'dim' && row.text.trim() ? row.text : ' '));
    const brightLines = aligned.map((row) => (row.layer === 'bright' && row.text.trim() ? row.text : ' '));

    const elapsedSeconds = this.thinkingSince !== undefined
      ? Math.max(0, Math.floor((Date.now() - this.thinkingSince) / 1000))
      : undefined;

    const headRaw = desired.status || `JOAT · ${terminalHeadTitle(desired.model.focus)}`;
    const headText = fitUtf8(
      oneMeasuredTerminalLine(stripTerminalModelIds(headRaw), NATIVE_TERMINAL_GEOMETRY.headTextWidth, this.measureTextWidth),
      TEXT_CONTAINER_MAX_BYTES,
    );
    const dimText = fitUtf8(dimLines.join('\n'), TEXT_CONTAINER_MAX_BYTES);
    const brightText = fitUtf8(brightLines.join('\n'), TEXT_CONTAINER_MAX_BYTES);
    const footText = fitUtf8(
      nativeTerminalFooter(desired.model, view.older, view.newer, {
        promptVisible: prompt !== undefined,
        promptTranscribing: prompt?.transcribing === true,
        elapsedSeconds,
        spinnerIndex: this.spinnerIndex,
        measureTextWidth: this.measureTextWidth,
      }),
      TEXT_CONTAINER_MAX_BYTES,
    );

    return {
      headText,
      dimLines,
      brightLines,
      dimText,
      brightText,
      footText,
      conversationKey: this.conversationKey(desired.model),
      older: view.older,
      newer: view.newer,
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
        ? await this.upgradeContainers(snapshot)
        : await this.rebuild(snapshot);
      if (!painted && this.pageMounted && this.snapshotCurrent(snapshot)) {
        this.pageMounted = false;
        this.physicalHeadText = '';
        this.physicalDimText = '';
        this.physicalBrightText = '';
        this.physicalFootText = '';
        painted = await this.rebuild(snapshot);
      }
      if (!this.snapshotCurrent(snapshot)) continue;
      this.lastPaintSucceeded = painted;
      if (!painted) continue;
      this.pageMounted = true;
      this.renderedToken = desired.token;
      this.renderedPromptId = prompt?.id;
      this.committedPage = page;
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
        this.physicalHeadText = snapshot.page.headText;
        this.physicalDimText = snapshot.page.dimText;
        this.physicalBrightText = snapshot.page.brightText;
        this.physicalFootText = snapshot.page.footText;
      }
      return replaced;
    } catch { return false; }
  }

  private async upgradeContainers(snapshot: RenderSnapshot): Promise<boolean> {
    if (!this.snapshotCurrent(snapshot)) return false;
    if (!this.imageHeader() && this.physicalHeadText !== snapshot.page.headText) {
      const ok = await this.upgradeOne(
        NativeTerminalContainer.Head.id,
        NativeTerminalContainer.Head.name,
        snapshot.page.headText,
        this.physicalHeadText,
      );
      if (!ok) return false;
      if (this.active && this.desired?.visit === snapshot.desired.visit) {
        this.physicalHeadText = snapshot.page.headText;
      }
      if (!this.snapshotCurrent(snapshot)) return false;
    }

    if (this.physicalDimText !== snapshot.page.dimText) {
      const ok = await this.upgradeOne(
        NativeTerminalContainer.Dim.id,
        NativeTerminalContainer.Dim.name,
        snapshot.page.dimText,
        this.physicalDimText,
      );
      if (!ok) return false;
      if (this.active && this.desired?.visit === snapshot.desired.visit) {
        this.physicalDimText = snapshot.page.dimText;
      }
      if (!this.snapshotCurrent(snapshot)) return false;
    }

    if (this.physicalBrightText !== snapshot.page.brightText) {
      const ok = await this.upgradeOne(
        NativeTerminalContainer.Bright.id,
        NativeTerminalContainer.Bright.name,
        snapshot.page.brightText,
        this.physicalBrightText,
      );
      if (!ok) return false;
      if (this.active && this.desired?.visit === snapshot.desired.visit) {
        this.physicalBrightText = snapshot.page.brightText;
      }
      if (!this.snapshotCurrent(snapshot)) return false;
    }

    if (this.physicalFootText !== snapshot.page.footText) {
      const ok = await this.upgradeOne(
        NativeTerminalContainer.Foot.id,
        NativeTerminalContainer.Foot.name,
        snapshot.page.footText,
        this.physicalFootText,
      );
      if (!ok) return false;
      if (this.active && this.desired?.visit === snapshot.desired.visit) {
        this.physicalFootText = snapshot.page.footText;
      }
    }

    return true;
  }

  private async upgradeOne(
    containerID: number,
    containerName: string,
    nextContent: string,
    previousContent: string,
  ): Promise<boolean> {
    const content = fitUtf8(nextContent, TEXT_CONTAINER_MAX_BYTES) || ' ';
    try {
      return await this.client.require().textContainerUpgrade(
        new TextContainerUpgrade({
          containerID,
          containerName,
          contentOffset: 0,
          contentLength: Math.max(previousContent.length, content.length),
          content,
        }),
      );
    } catch {
      return false;
    }
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
    this.physicalHeadText = '';
    this.physicalDimText = '';
    this.physicalBrightText = '';
    this.physicalFootText = '';
    this.thinkingSince = undefined;
    this.spinnerIndex = 2;
    this.lastPaintSucceeded = false;
    this.renderedPromptId = undefined;
    this.committedPage = undefined;
  }
}
