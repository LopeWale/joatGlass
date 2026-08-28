import { describe, expect, it } from 'vitest';
import {
  HUD_VISUAL_GEOMETRY,
  gray8ToGray4,
  hudBrightnessGray,
  hudDisplayTitle,
  parseHudLabeledValue,
  parseHudListItem,
  resolveHudOverview,
  visibleHudListItems,
  visibleHudTailLines,
  wrapHudText,
} from './HudCanvasRenderer';

describe('hudBrightnessGray', () => {
  it('keeps routine and urgent Mac cards visually distinct', () => {
    expect(hudBrightnessGray(3)).toBeLessThan(hudBrightnessGray(4));
    expect(hudBrightnessGray(3)).toBe(170);
    expect(hudBrightnessGray(4)).toBe(238);
  });

  it('quantizes the authored canvas into the G2 raw 4-bit range', () => {
    expect(gray8ToGray4(0)).toBe(0);
    expect(gray8ToGray4(51)).toBe(3);
    expect(gray8ToGray4(170)).toBe(10);
    expect(gray8ToGray4(238)).toBe(14);
    expect(gray8ToGray4(255)).toBe(15);
  });
});

describe('selected visual geometry', () => {
  it('pins the measured reference anchors instead of a generic wide menu', () => {
    expect(HUD_VISUAL_GEOMETRY).toMatchObject({
      statusDividerY: 35.5,
      overviewMainX: 176,
      overviewTrayY: 168,
      sidebarWidth: 160,
      sidebarBackdropOffsetX: 78,
      listContentX: 93,
      confirmationActionX: 194,
      confirmationProgressX: 97,
      confirmationProgressWidth: 367,
    });
  });

  it('uses the reference sentence-case focus heading', () => {
    expect(hudDisplayTitle('MESSAGES · 3')).toBe('Messages · 3');
    expect(hudDisplayTitle('MESSAGES · SETUP')).toBe('Messages · Setup');
    expect(hudDisplayTitle('JOAT')).toBe('JOAT');
    expect(hudDisplayTitle('NOTIFICATIONS')).toBe('Notifications');
    expect(hudDisplayTitle('Already Mixed')).toBe('Already Mixed');
  });
});

describe('parseHudLabeledValue', () => {
  it('treats a normal assistant reply as content, never as a label', () => {
    expect(parseHudLabeledValue('Your meeting starts in 19 minutes.', 'REPLY')).toEqual({
      label: 'REPLY',
      value: 'Your meeting starts in 19 minutes.',
      structured: false,
    });
  });

  it('preserves the approved label/value fixture shape', () => {
    expect(parseHudLabeledValue('NEXT\nDesign review · 4:30', 'REPLY')).toEqual({
      label: 'NEXT',
      value: 'Design review · 4:30',
      structured: true,
    });
  });
});

describe('resolveHudOverview', () => {
  it('turns a status-only connecting boot into one intentional focus state', () => {
    expect(resolveHudOverview({
      status: '4:28   JOAT connecting',
      main: '  ',
      context: '',
      tray: '\n',
    })).toEqual({
      kind: 'empty',
      title: 'Connecting',
      primary: 'Waiting for JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: 'left x2: menu · long-press: mic',
    });
  });

  it('never mixes stale Overview rows into a reconnecting frame', () => {
    expect(resolveHudOverview({
      status: '4:28   JOAT reconnecting',
      main: 'REPLY\nOld answer from the prior connection',
      context: 'LAST QUERY\nold query',
      tray: '2 notifications',
    })).toEqual({
      kind: 'empty',
      title: 'Reconnecting',
      primary: 'Restoring JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: 'left x2: menu · long-press: mic',
    });
  });

  it('never invents tray or context chrome for a partial main-only frame', () => {
    expect(resolveHudOverview({
      status: '9:41   JOAT mic off',
      main: 'Your meeting starts in 19 minutes.',
      context: '',
      tray: '',
    })).toEqual({
      kind: 'content',
      main: {
        label: 'REPLY',
        value: 'Your meeting starts in 19 minutes.',
        structured: false,
      },
    });
  });

  it('preserves every semantic section in the populated Overview fixture', () => {
    expect(resolveHudOverview({
      status: '9:41   JOAT ready',
      main: 'NEXT\nDesign review · 4:30',
      context: 'LAST QUERY\nnearest coffee shop?',
      tray: '2 notifications waiting',
    })).toEqual({
      kind: 'content',
      main: {
        label: 'NEXT',
        value: 'Design review · 4:30',
        structured: true,
      },
      tray: '2 notifications',
      context: {
        label: 'LAST QUERY',
        value: 'nearest coffee shop?',
        structured: true,
      },
    });
  });
});

describe('parseHudListItem', () => {
  it('extracts only a real Mac-provided age from a message row', () => {
    expect(parseHudListItem('[18m] Marcus · Heading to the lab in 10.')).toEqual({
      primary: 'Marcus',
      secondary: 'Heading to the lab in 10.',
      age: '18m',
    });
  });

  it('does not invent an age for loading, empty, or settings rows', () => {
    expect(parseHudListItem('Loading recent notifications…')).toEqual({
      primary: 'Loading recent notifications…',
      secondary: '',
      age: '',
    });
  });
});

describe('visibleHudListItems', () => {
  it('keeps later Mac rows reachable on the three-row optical page', () => {
    const items = ['one', 'two', 'three', 'four', 'five'];
    expect(visibleHudListItems(items, 0)).toEqual([
      { item: 'one', index: 0 },
      { item: 'two', index: 1 },
      { item: 'three', index: 2 },
    ]);
    expect(visibleHudListItems(items, 4)).toEqual([
      { item: 'three', index: 2 },
      { item: 'four', index: 3 },
      { item: 'five', index: 4 },
    ]);
  });
});

describe('HUD text visibility', () => {
  it('keeps the newest streamed reply lines visible after overflow', () => {
    expect(visibleHudTailLines(['already', 'read', 'newest', 'tail'], 2))
      .toEqual(['…newest', 'tail']);
  });

  it('wraps the complete maximum-length confirmation without an ellipsis', () => {
    const prompt = 'Send the confidential design review summary to Adele and Marcus after the meeting including every requested attachment?';
    expect(prompt.length).toBeLessThanOrEqual(120);
    const lines = wrapHudText(prompt, 44, (value) => value.length);
    expect(lines.length).toBeLessThanOrEqual(3);
    expect(lines.join(' ')).toBe(prompt);
    expect(lines.join('')).not.toContain('…');
  });
});
