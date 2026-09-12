import { describe, expect, it } from 'vitest';
import { wrapTerminalParagraph } from './TerminalText';

describe('literal terminal text wrapping', () => {
  it('preserves code indentation, blank paragraphs, repeated spaces and tab expansion', () => {
    const text = '  def hello():\n\tprint("hello  world")\n\n    return 42  ';
    const paragraphs = text.split('\n');
    const wrapped = paragraphs.map(paragraph => wrapTerminalParagraph(paragraph, 16, value => value.length));
    expect(wrapped.map(rows => rows.join('')).join('\n')).toBe(text.replace(/\t/g,'    '));
    expect(wrapped[0]?.[0]?.startsWith('  ')).toBe(true);
    expect(wrapped[2]).toEqual(['']);
  });

  it('prefers word boundaries while preserving exact separators', () => {
    expect(wrapTerminalParagraph('one  two three', 9, value => value.length)).toEqual(['one  two ', 'three']);
  });

  it('handles long Unicode tokens without oversized or quadratic measurements', () => {
    const token = 'abc😀'.repeat(6000);
    let calls = 0; let largest = 0;
    const lines = wrapTerminalParagraph(token, 45, value => {
      calls++; largest = Math.max(largest, value.length); return Array.from(value).length;
    });
    expect(lines.join('')).toBe(token);
    expect(lines.every(line => !/[\uD800-\uDBFF]$/.test(line))).toBe(true);
    expect(calls).toBeLessThan(lines.length * 16);
    expect(largest).toBeLessThan(160);
  });
});
