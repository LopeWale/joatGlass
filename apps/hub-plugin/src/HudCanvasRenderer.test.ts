import { describe, expect, it } from 'vitest';
import {
  ASSISTANT_OVERLAY_DISMISS_HINT,
  NOTIFICATION_DISMISS_HINT,
  assistantOverlayTextLayout,
  HUD_VISUAL_GEOMETRY,
  brainstormActionHint,
  brainstormStateLabel,
  hudBrightnessGray,
  hudDisplayTitle,
  hudListItemIconName,
  hudSidebarIconName,
  parseHudLabeledValue,
  parseHudListItem,
  parseMessagesConversationRow,
  usesMessagesInstructionCards,
  canRenderPlanningMonthGrid,
  fitHudWrappedText,
  planningGridMarker,
  planningGridMatchesMonth,
  parsePlanningMonthTitle,
  planningMonthDays,
  productivityActionHint,
  productivityPhaseLabel,
  productivityPhaseProgress,
  resolveHudOverview,
  resolveHudListKind,
  showNavigationSecondaryInHeader,
  visibleHudSidebarItems,
  visibleHudListItems,
  visibleHudLeadingLines,
  visibleHudTailLines,
  wrapHudText,
} from './HudCanvasRenderer';

describe('hudBrightnessGray', () => {
  it('keeps routine and urgent Mac cards visually distinct', () => {
    expect(hudBrightnessGray(3)).toBeLessThan(hudBrightnessGray(4));
    expect(hudBrightnessGray(3)).toBe(170);
    expect(hudBrightnessGray(4)).toBe(238);
  });

});

describe('bounded HUD copy', () => {
  it('keeps the beginning of an incoming notification preview visible', () => {
    expect(visibleHudLeadingLines(['Sender and subject', 'First sentence', 'Older tail'], 2))
      .toEqual(['Sender and subject', 'First sentence…']);
  });
});

describe('productivity escape copy', () => {
  it.each([
    [{ phase: 'verifying', monitoringArmed: false }, 'Back: left x2 to Planning'],
    [{ phase: 'saved', monitoringArmed: true }, 'Back: left x2 to Planning · monitoring next'],
    [{ phase: 'monitoring', monitoringArmed: false }, 'Back: left x2 to Planning · auto in 5s'],
    [{ phase: 'completed', monitoringArmed: false }, 'Back: left x2 to Planning · auto in 5s'],
    [{ phase: 'failed', monitoringArmed: false }, 'Back: left x2 to Planning · auto in 5s'],
    [{ phase: 'check', monitoringArmed: false }, 'Back: left x2 to Planning · auto in 5s'],
    [{ phase: 'cancelled', monitoringArmed: false }, 'Back: left x2 to Planning · auto in 5s'],
    [{ phase: 'not_saved', monitoringArmed: false }, 'Back: left x2 to Planning · auto in 5s'],
    [{ phase: 'due', monitoringArmed: false }, 'say: complete reminder · Back: left x2 to Planning'],
  ] as const)('keeps %o visibly escapable', (model, hint) => {
    expect(productivityActionHint(model)).toBe(hint);
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
      confirmationActionsY: 178,
      confirmationPrimaryX: 112,
      confirmationSecondaryX: 294,
      confirmationActionWidth: 170,
      confirmationActionHeight: 36,
      confirmationProgressX: 97,
      confirmationProgressWidth: 367,
    });
  });

  it('uses the reference sentence-case focus heading', () => {
    expect(hudDisplayTitle('MESSAGES · 3')).toBe('Messages · 3');
    expect(hudDisplayTitle('MESSAGES · SETUP')).toBe('Messages · Setup');
    expect(hudDisplayTitle('JOAT')).toBe('JOAT');
    expect(hudDisplayTitle('NOTIFICATIONS')).toBe('Notifications');
    expect(hudDisplayTitle('QUICKNOTES')).toBe('QuickNotes');
    expect(hudDisplayTitle('PLANNING · PLAN · SEPTEMBER 2026')).toBe('Planning · Month · September 2026');
    expect(hudDisplayTitle('PLANNING · PLAN · AUG 31–SEP 6')).toBe('Planning · Week · Aug 31–Sep 6');
    expect(hudDisplayTitle('PLANNING · PLAN · TUE SEP 1')).toBe('Planning · Day · Tue Sep 1');
    expect(hudDisplayTitle('PLANNING · PLAN · SETUP')).toBe('Planning · Setup');
    expect(hudDisplayTitle('Already Mixed')).toBe('Already Mixed');
  });
});

describe('semantic HUD chrome', () => {
  it('labels the assistant overlay with actions that actually dismiss it', () => {
    expect(ASSISTANT_OVERLAY_DISMISS_HINT).toBe('TAP OR SWIPE TO DISMISS');
  });

  it('does not advertise the right-temple exit gesture as notification navigation', () => {
    expect(NOTIFICATION_DISMISS_HINT).toBe('tap: dismiss · left x2: menu');
  });

  it('maps sidebar glyphs by capability name rather than stale row position', () => {
    expect(hudSidebarIconName('Messages')).toBe('messages');
    expect(hudSidebarIconName('Notifications')).toBe('notifications');
    expect(hudSidebarIconName('Planning')).toBe('calendar');
    expect(hudSidebarIconName('Calendar')).toBe('calendar');
    expect(hudSidebarIconName('Tasks')).toBe('tasks');
    expect(hudSidebarIconName('Navigation')).toBe('navigation');
    expect(hudSidebarIconName('More')).toBe('more');
    expect(hudSidebarIconName('QuickNotes')).toBe('quicknotes');
    expect(hudSidebarIconName('Brainstorm')).toBe('brainstorm');
    expect(hudSidebarIconName('Activity')).toBe('activity');
  });

  it('resolves list treatment from the Mac-owned capability title', () => {
    expect(resolveHudListKind('MESSAGES')).toBe('messages');
    expect(resolveHudListKind('NOTIFICATIONS')).toBe('notifications');
    expect(resolveHudListKind('PLANNING · PLAN · SEP 1–7')).toBe('calendar');
    expect(resolveHudListKind('CALENDAR · 7 DAYS')).toBe('calendar');
    expect(resolveHudListKind('TASKS')).toBe('tasks');
    expect(resolveHudListKind('QUICKNOTES')).toBe('quicknotes');
    expect(resolveHudListKind('ACTIVITY')).toBe('activity');
    expect(resolveHudListKind('ACTION · VERIFYING')).toBe('activity');
    expect(resolveHudListKind('LISTENING RESUME')).toBe('generic');
  });

  it('keeps mixed Planning agenda sources visible in their row glyphs', () => {
    expect(hudListItemIconName(
      'PLANNING · PLAN · TUE SEP 1',
      '10:30 · EVENT · Design review',
    )).toBe('calendar');
    expect(hudListItemIconName(
      'PLANNING · PLAN · TUE SEP 1',
      '19:00 · REMINDER · Buy filters',
    )).toBe('tasks');
  });

});

describe('navigation error hierarchy', () => {
  it('keeps the recovery instruction in the warning card instead of rendering it twice', () => {
    expect(showNavigationSecondaryInHeader('destination')).toBe(true);
    expect(showNavigationSecondaryInHeader('mode')).toBe(true);
    expect(showNavigationSecondaryInHeader('error')).toBe(false);
  });
});

describe('assistant overlay text fitting', () => {
  it('keeps a four-line assistant reply below the header divider and above its footer', () => {
    const layout = assistantOverlayTextLayout(4, 17);

    expect(layout).toMatchObject({
      dividerY: 92,
      firstGlyphTopY: 109,
      lastGlyphBottomY: 183,
      footerBaselineY: 214,
    });
    expect(layout.firstGlyphTopY).toBeGreaterThan(layout.dividerY);
    expect(layout.lastGlyphBottomY).toBeLessThan(layout.footerBaselineY);
  });

  it('keeps only the newest four lines when the minimum font still overflows', () => {
    const fitted = fitHudWrappedText(
      'one two three four five six seven eight nine ten eleven twelve thirteen fourteen',
      72,
      4,
      (value, fontSize) => value.length * fontSize * 0.5,
    );

    expect(fitted.fontSize).toBe(12);
    expect(fitted.lines).toHaveLength(4);
    expect(fitted.lines[0]).toMatch(/^…/);
    expect(fitted.lines.join(' ')).toContain('fourteen');
  });
});

describe('Planning month geometry', () => {
  it('recognizes the native Planning month title and creates a stable Monday-first grid', () => {
    const month = parsePlanningMonthTitle('PLANNING · PLAN · SEPTEMBER 2026');

    expect(month).toEqual({ year: 2026, month: 8, label: 'SEPTEMBER' });
    expect(planningMonthDays(month!).slice(0, 7)).toEqual([
      { day: 31, inMonth: false },
      { day: 1, inMonth: true },
      { day: 2, inMonth: true },
      { day: 3, inMonth: true },
      { day: 4, inMonth: true },
      { day: 5, inMonth: true },
      { day: 6, inMonth: true },
    ]);
    expect(planningMonthDays(month!)).toHaveLength(42);
    const days = planningMonthDays(month!);
    expect(days[days.length - 1]).toEqual({ day: 11, inMonth: false });

    const planningGrid = Array.from({ length: 42 }, (_, index) => {
      const value = new Date(Date.UTC(2026, 7, 31 + index));
      return {
        day: value.toISOString().slice(0, 10),
        eventCount: index === 1 ? 1 : 0,
        reminderCount: index === 1 ? 1 : 0,
        inMonth: value.getUTCMonth() === 8,
      };
    });
    expect(planningGridMatchesMonth(month!, planningGrid)).toBe(true);
    expect(planningGridMarker(planningGrid[1]!)).toBe('E1 R1');
    expect(planningGridMarker(planningGrid[0]!)).toBe('');
    expect(canRenderPlanningMonthGrid(month, planningGrid, true, 0, 11)).toBe(true);
    expect(canRenderPlanningMonthGrid(month, planningGrid, true, 5, 11)).toBe(true);
    expect(canRenderPlanningMonthGrid(month, planningGrid, true, 6, 11)).toBe(false);
    expect(planningGridMatchesMonth(month!, [
      ...planningGrid.slice(0, 1),
      { ...planningGrid[1]!, day: '2026-09-02' },
      ...planningGrid.slice(2),
    ])).toBe(false);
  });

  it('does not mistake a week, day, or arbitrary list for a calendar month', () => {
    expect(parsePlanningMonthTitle('PLANNING · PLAN · SEP 1–7')).toBeUndefined();
    expect(parsePlanningMonthTitle('PLANNING · PLAN · TUE SEP 1')).toBeUndefined();
    expect(parsePlanningMonthTitle('SEPTEMBER 2026')).toBeUndefined();
    expect(parsePlanningMonthTitle('PLANNING · PLAN · JANUARY 0099')).toBeUndefined();
  });
});

describe('productivity HUD state labels', () => {
  it('keeps native verification, uncertainty, and due alarms visually distinct', () => {
    expect(productivityPhaseLabel('verifying')).toBe('VERIFYING');
    expect(productivityPhaseLabel('saved')).toBe('SAVED');
    expect(productivityPhaseLabel('monitoring')).toBe('MONITORING');
    expect(productivityPhaseLabel('completed')).toBe('COMPLETED');
    expect(productivityPhaseLabel('failed')).toBe('FAILED');
    expect(productivityPhaseLabel('check')).toBe('UNCERTAIN');
    expect(productivityPhaseLabel('due')).toBe('DUE NOW');
    expect(productivityPhaseProgress('saved')).toBe(1);
    expect(productivityPhaseProgress('monitoring')).toBe(1);
    expect(productivityPhaseProgress('completed')).toBe(1);
    expect(productivityPhaseProgress('due')).toBe(1);
    expect(productivityPhaseProgress('verifying')).toBe(0.45);
    expect(productivityPhaseProgress('check')).toBe(0.18);
  });
});

describe('Brainstorm wearable actions', () => {
  it('discloses both physical cancellation gestures while a session is running', () => {
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'running',
      terminalState: 'ready',
      noToolsChecked: true,
    })).toBe('tap: Hermes · swipe down or left x2: cancel');
  });

  it('keeps the diagnostic test gesture off an ordinary ready Brainstorm', () => {
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'ready',
      terminalState: 'ready',
      noToolsChecked: true,
    })).toBe('tap: speak · left x2: menu');
  });

  it('shows the diagnostic test gesture only when the Mac explicitly arms it', () => {
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'ready',
      terminalState: 'ready',
      noToolsChecked: true,
      acceptanceArmed: true,
    } as Parameters<typeof brainstormActionHint>[0])).toBe('tap: speak · right/ring x2: test · left x2: menu');
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'ready',
      terminalState: 'blocked',
      noToolsChecked: false,
    })).not.toContain('test');
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'acceptance_ready',
      terminalState: 'ready',
      noToolsChecked: true,
    })).toBe('tap: start test · left x2: menu');
    expect(brainstormStateLabel('acceptance_ready')).toBe('TEST READY');
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'topic_capture',
      terminalState: 'ready',
      noToolsChecked: true,
    })).toBe('speak your question · left x2: cancel');
    expect(brainstormStateLabel('topic_capture')).toBe('SPEAK NOW');
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

  it('labels a typed incoming Messages card as MESSAGE, never REPLY', () => {
    expect(resolveHudOverview({
      status: '9:41   JOAT listening',
      main: '(201) 462-3963 · Messages\nCajun Corner order confirmed',
      mainKind: 'notification',
      context: '',
      tray: '',
    })).toEqual({
      kind: 'content',
      main: {
        label: 'MESSAGE',
        value: '(201) 462-3963 · Messages Cajun Corner order confirmed',
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

describe('parseMessagesConversationRow', () => {
  it('uses instruction cards for conversation and compose surfaces only', () => {
    expect(usesMessagesInstructionCards('MESSAGES · CONVERSATION')).toBe(true);
    expect(usesMessagesInstructionCards('MESSAGES · COMPOSE')).toBe(true);
    expect(usesMessagesInstructionCards('MESSAGES')).toBe(false);
    expect(usesMessagesInstructionCards('MESSAGES · ACCESS')).toBe(false);
  });

  it('separates incoming, outgoing, and voice-action rows', () => {
    expect(parseMessagesConversationRow('Dele · Are you coming?')).toEqual({
      kind: 'incoming', label: 'Dele', body: 'Are you coming?',
    });
    expect(parseMessagesConversationRow('You · On my way')).toEqual({
      kind: 'outgoing', label: 'You', body: 'On my way',
    });
    expect(parseMessagesConversationRow('Say “JOAT, reply saying …”')).toEqual({
      kind: 'instruction', label: '', body: 'Say “JOAT, reply saying …”',
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

describe('visibleHudSidebarItems', () => {
  it('keeps the selected capability visible beyond the first five wearable rows', () => {
    const items = [
      'Messages', 'Planning', 'Navigation', 'Brainstorm', 'Notifications',
      'QuickNotes', 'Agent Terminal', 'Activity',
    ];
    expect(visibleHudSidebarItems(items, 0)).toEqual([
      { item: 'Messages', index: 0 },
      { item: 'Planning', index: 1 },
      { item: 'Navigation', index: 2 },
      { item: 'Brainstorm', index: 3 },
      { item: 'Notifications', index: 4 },
    ]);
    expect(visibleHudSidebarItems(items, 7)).toEqual([
      { item: 'Brainstorm', index: 3 },
      { item: 'Notifications', index: 4 },
      { item: 'QuickNotes', index: 5 },
      { item: 'Agent Terminal', index: 6 },
      { item: 'Activity', index: 7 },
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
