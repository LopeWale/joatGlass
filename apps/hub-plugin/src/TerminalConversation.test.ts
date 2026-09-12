import { describe, expect, it } from 'vitest';
import { TerminalConversationViewport } from './TerminalConversation';
import { wrapHudText } from './HudCanvasRenderer';
import { wrapTerminalParagraph } from './TerminalText';
import type { TerminalConversationMessage } from './types';
const wrap = (text: string) => wrapTerminalParagraph(text, 45, value => value.length);
const reply = (id: string, text: string): TerminalConversationMessage => ({id, role:'assistant', text});

describe('persistent terminal viewport', () => {
  it('makes the complete beginning middle and end of long paragraphs reachable', () => {
    const viewport = new TerminalConversationViewport();
    const text = `BEGIN ${'first paragraph words '.repeat(180)}\n\nMIDDLE ${'second paragraph words '.repeat(180)}\n\nEND`;
    const messages = [reply('reply', text)];
    const seen = new Set<string>();
    const render = () => viewport.view('openclaw', messages, 12, wrap);
    let page = render();
    expect(page.lines.join(' ')).toContain('END');
    page.lines.forEach(line => seen.add(line));
    while (viewport.scroll('up')) {
      page = render();
      page.lines.forEach(line => seen.add(line));
    }
    expect([...seen].join(' ')).toContain('BEGIN');
    expect([...seen].join(' ')).toContain('MIDDLE');
    expect(page.lines[0]).toContain('BEGIN');
  });

  it('keeps the reader anchored across status, append, agent switch and reopen', () => {
    const viewport = new TerminalConversationViewport();
    const messages = [reply('one', Array.from({length:80}, (_,i) => `Line ${i}`).join('\n'))];
    const render = (rows=messages) => viewport.view('openclaw', rows, 12, wrap);
    render(); viewport.scroll('up'); viewport.scroll('up');
    const before = render().lines;
    expect(render().lines).toEqual(before); // working/ready does not change identity
    const appended = [...messages, reply('two', 'A new reply\nMore new output')];
    expect(render(appended).lines).toEqual(before);
    viewport.view('hermes', [reply('other','Other agent')], 12, wrap);
    expect(render(appended).lines).toEqual(before);
    while (viewport.scroll('down')) render(appended);
    expect(render([...appended, reply('three','Latest reply')]).lines.join(' ')).toContain('Latest reply');
  });

  it('wraps very long unbroken Unicode tokens with bounded canvas measurements', () => {
    const token = 'abc😀'.repeat(6000);
    let calls = 0;
    let largestMeasurement = 0;
    const lines = wrapHudText(token, 45, text => {
      calls++;
      largestMeasurement = Math.max(largestMeasurement, text.length);
      return Array.from(text).length;
    });
    expect(lines.join('')).toBe(token);
    expect(lines.every(line => !/[\uD800-\uDBFF]$/.test(line))).toBe(true);
    expect(calls).toBeLessThan(lines.length * 16);
    expect(largestMeasurement).toBeLessThan(160);
  });

  it('reuses measured messages on status paints and only wraps changed or appended text', () => {
    const viewport = new TerminalConversationViewport();
    let calls = 0;
    const measured = (text: string) => { calls++; return wrap(text); };
    const messages = [reply('one', 'First paragraph\nSecond paragraph')];
    viewport.view('agent', messages, 12, measured, false, '550|13px');
    const initialCalls = calls;
    viewport.view('agent', messages.map(message => ({...message})), 10, measured, false, '550|13px');
    expect(calls).toBe(initialCalls);
    viewport.view('agent', [...messages, reply('two', 'New answer')], 12, measured, false, '550|13px');
    expect(calls).toBe(initialCalls + 1);
    viewport.view('agent', [...messages, reply('two', 'Edited answer')], 12, measured, false, '550|13px');
    expect(calls).toBe(initialCalls + 2);
    viewport.view('agent', messages, 12, measured, false, '400|13px');
    expect(calls).toBe(initialCalls * 2 + 2);
  });

  it('exposes history eviction and resets only a new conversation identity', () => {
    const viewport = new TerminalConversationViewport();
    const messages = Array.from({length:70},(_,i)=>reply(`m${i}`, `Message ${i}`));
    viewport.view('old', messages, 12, wrap);
    while (viewport.scroll('up')) viewport.view('old',messages,12,wrap);
    expect(viewport.view('old',messages,12,wrap).lines[0]).toContain('Earlier conversation history');
    expect(viewport.view('new',messages,12,wrap).lines.join(' ')).toContain('Message 69');
  });
});
