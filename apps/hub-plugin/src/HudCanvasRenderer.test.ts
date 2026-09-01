import { describe, expect, it } from 'vitest';
import * as HudCanvas from './HudCanvasRenderer';
import {
  ASSISTANT_OVERLAY_DISMISS_HINT,
  NOTIFICATION_DISMISS_HINT,
  agentTerminalSafetyLabel,
  agentTerminalFocusLabel,
  agentTerminalTranscriptLayout,
  assistantOverlayTextLayout,
  brainstormTranscriptLines,
  HUD_VISUAL_GEOMETRY,
  brainstormActionHint,
  brainstormStateLabel,
  hudBrightnessGray,
  hudDisplayTitle,
  hudSidebarIconName,
  parseHudLabeledValue,
  parseHudListItem,
  parseMessagesConversationRow,
  messagesConversationFooterFontSize,
  messagesReaderBodyLines,
  messagesFullTextContent,
  MESSAGES_READER_BODY_LINE_CAPACITY,
  messagesConversationTranscript,
  navigationDecisionRows,
  usesNavigationSetupLayout,
  usesMessagesCompactLayout,
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

describe('Messages full-text reader', () => {
  it('preserves the leading body page as flat text instead of a card', () => {
    expect(messagesFullTextContent([
      'From Dele · unread · 1/2',
      'Opening words remain visible',
      'on this page.',
    ])).toEqual({
      metadata: 'From Dele · unread · 1/2',
      body: 'Opening words remain visible on this page.',
    });
  });

  it('uses the band reclaimed from the title bar for two more body lines', () => {
    expect(MESSAGES_READER_BODY_LINE_CAPACITY).toBe(10);
  });

  it('wraps the reader body by conservative visible columns, not the simulator width estimate', () => {
    expect(messagesReaderBodyLines(
      'Read reply with full reader detail with full reader detail',
    )).toEqual([
      'Read reply with full reader detail with full reader',
      'detail',
    ]);
  });

  it('uses the compact footer face when all real conversation controls are present', () => {
    expect(messagesConversationFooterFontSize('down older · tap read · say reply · left x2: back')).toBe(8);
    expect(messagesConversationFooterFontSize('left x2: back')).toBe(9);
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

describe('flat productivity chrome', () => {
  it('collapses confirmation controls into one static low-bandwidth footer', () => {
    expect(HudCanvas.confirmationActionHint()).toBe(
      'tap: confirm · swipe: cancel · left x2: back · auto-cancel',
    );
  });

  it('uses the same compact left reading axis as the other first-level HUDs', () => {
    expect(HudCanvas.confirmationReviewLayout()).toEqual({
      headerIconX: 36,
      headerTextX: 62,
      headerBaselineY: 60,
      dividerY: 78.5,
      contentX: 36,
      contentWidth: 504,
      promptTopBaselineY: 104,
      promptFontSize: 16,
      promptLineHeight: 20,
      promptMaxLines: 4,
      footerBaselineY: 267,
    });
  });

  it('removes the outer rounded card from every list and flow page', () => {
    expect(HudCanvas.usesFlatHudShell('confirmation')).toBe(true);
    expect(HudCanvas.usesFlatHudShell('productivity')).toBe(true);
    expect(HudCanvas.usesFlatHudShell('list', 'PLANNING · PLAN · TUE SEP 1')).toBe(true);
    // The wearer's ruling: the card was the waste, not its border. Every
    // list page is flat now — Messages, QuickNotes, and all the rest.
    expect(HudCanvas.usesFlatHudShell('list', 'MESSAGES')).toBe(true);
    expect(HudCanvas.usesFlatHudShell('list', 'QUICKNOTES')).toBe(true);
    expect(HudCanvas.usesFlatHudShell('brainstorm')).toBe(false);
  });

  it('renders stationary navigation pages flat; only moving states keep the shell', () => {
    // Text pages a wearer reads while stopped carried the rounded card too
    // (the wearer's ruling: remove it). The moving live-session states stay
    // in the shell family with Brainstorm.
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'destination')).toBe(true);
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'repair')).toBe(true);
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'mode')).toBe(true);
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'preview')).toBe(true);
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'arrived')).toBe(true);
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'error')).toBe(true);
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'guidance')).toBe(false);
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'uncertain')).toBe(false);
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'wrong_way')).toBe(false);
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'off_route')).toBe(false);
    expect(HudCanvas.usesFlatHudShell('navigation', '', 'rerouting')).toBe(false);
    expect(HudCanvas.usesFlatHudShell('navigation')).toBe(false);
  });
});

describe('selected visual geometry', () => {
  it('pins the measured reference anchors instead of a generic wide menu', () => {
    expect(HUD_VISUAL_GEOMETRY).toMatchObject({
      statusDividerY: 35.5,
      overviewMainX: 176,
      overviewTrayY: 168,
      // The menu occupies the clock column exactly: no stranded gap
      // between its edge and the content, and the content never moves.
      sidebarWidth: 256,
      listContentX: 93,
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

});

describe('navigation error hierarchy', () => {
  it('keeps the recovery instruction in the warning card instead of rendering it twice', () => {
    expect(showNavigationSecondaryInHeader('destination')).toBe(true);
    expect(showNavigationSecondaryInHeader('mode')).toBe(true);
    expect(showNavigationSecondaryInHeader('error')).toBe(false);
  });
});

describe('navigation decisions', () => {
  it('boxes the chosen row at a size readable in motion', () => {
    // A route choice is read while walking, so it uses the legible face and
    // an outline on the SELECTED row — not the dense reading size a
    // stationary list can use, and not a box per row.
    expect(navigationDecisionRows(2)).toEqual([
      { baselineY: 128, boxTopY: 113, boxHeight: 30, fontSize: 15 },
      { baselineY: 162, boxTopY: 147, boxHeight: 30, fontSize: 15 },
    ]);
    expect(navigationDecisionRows(3)).toEqual([
      { baselineY: 128, boxTopY: 113, boxHeight: 30, fontSize: 15 },
      { baselineY: 162, boxTopY: 147, boxHeight: 30, fontSize: 15 },
      { baselineY: 196, boxTopY: 181, boxHeight: 30, fontSize: 15 },
    ]);
    expect(navigationDecisionRows(0)).toEqual([]);
  });

  it('keeps every option and its box clear of the footer', () => {
    const rows = navigationDecisionRows(3);
    const last = rows[rows.length - 1]!;
    expect(last.boxTopY + last.boxHeight).toBeLessThan(267);
  });
});

describe('flat navigation entry', () => {
  it('reserves the compact instruction treatment for the non-selectable Navigation entry', () => {
    expect(usesNavigationSetupLayout('NAVIGATION', false)).toBe(true);
    expect(usesNavigationSetupLayout('NAVIGATION', true)).toBe(false);
    expect(usesNavigationSetupLayout('MESSAGES', false)).toBe(false);
  });
});

describe('compact focus list rows', () => {
  it('gives a Planning day the same ten dense rows as every other list', () => {
    // The two-line agenda variant existed only because a day row spelled out
    // "16:30 · EVENT · Title". Rows now read "[16:30] \u25a1 Title", so they
    // fit one line — and with the page name moved into the status spine,
    // rows start at y60 instead of y84: ten entries per glance.
    expect(HudCanvas.planningFocusRows('PLANNING · PLAN · TUE SEP 1', 4)).toEqual([
      { primaryBaselineY: 60, railTopY: 50, railHeight: 19 },
      { primaryBaselineY: 80, railTopY: 70, railHeight: 19 },
      { primaryBaselineY: 100, railTopY: 90, railHeight: 19 },
      { primaryBaselineY: 120, railTopY: 110, railHeight: 19 },
    ]);
    expect(HudCanvas.planningFocusRows('PLANNING · PLAN · TUE SEP 1', 12)).toHaveLength(10);
    // No row carries a second line any more.
    expect(
      HudCanvas.planningFocusRows('PLANNING · PLAN · TUE SEP 1', 4)[0]?.secondaryBaselineY,
    ).toBeUndefined();
  });

  it('uses the full vertical budget: ten one-line rows per glance', () => {
    // Pitch divides rows; separator lines, dead bands, and the in-body
    // title are gone. The wearer's ruling: unused canvas on a content page
    // is waste, not calm — and the page's name belongs in the spine.
    expect(HudCanvas.planningFocusRows('PLANNING · PLAN · AUG 31–SEP 6', 7)).toEqual([
      { primaryBaselineY: 60, railTopY: 50, railHeight: 19 },
      { primaryBaselineY: 80, railTopY: 70, railHeight: 19 },
      { primaryBaselineY: 100, railTopY: 90, railHeight: 19 },
      { primaryBaselineY: 120, railTopY: 110, railHeight: 19 },
      { primaryBaselineY: 140, railTopY: 130, railHeight: 19 },
      { primaryBaselineY: 160, railTopY: 150, railHeight: 19 },
      { primaryBaselineY: 180, railTopY: 170, railHeight: 19 },
    ]);
  });

  it('names the page in the status spine instead of a second title bar', () => {
    expect(HudCanvas.hudSpinePageTitle({
      kind: 'list', title: 'MESSAGES · CONVERSATION',
    } as never)).toBe('Messages · Conversation');
    expect(HudCanvas.hudSpinePageTitle({ kind: 'navigation' } as never)).toBe('Navigation');
    // Home is JOAT: the spine keeps the assistant's own name there.
    expect(HudCanvas.hudSpinePageTitle({ kind: 'overview' } as never)).toBe('');
    expect(HudCanvas.hudSpinePageTitle({ kind: 'sidebar' } as never)).toBe('');
  });

  it('recognizes only the small semantic marker vocabulary', () => {
    // Markers indicate a row's KIND (● recording, □ todo, – key point) from
    // the design-guidelines firmware-safe set; anything else is content.
    expect(HudCanvas.focusListMarker('● Client standup')).toEqual({
      glyph: '●', emphasis: 'content',
    });
    expect(HudCanvas.focusListMarker('□ Email release notes')).toEqual({
      glyph: '□', emphasis: 'annotation',
    });
    expect(HudCanvas.focusListMarker('– Ship Friday')).toEqual({
      glyph: '–', emphasis: 'annotation',
    });
    expect(HudCanvas.focusListMarker('Plain row')).toBeUndefined();
    expect(HudCanvas.focusListMarker('★ Unknown glyph')).toBeUndefined();
    expect(HudCanvas.focusListMarker('●NoSpace')).toBeUndefined();
  });

  it('reserves one shared title edge only for lists that mix marked rows', () => {
    // The QuickNotes list mixes recordings and text notes; unmarked rows
    // must indent so every title starts at the same x (no ragged edge).
    expect(HudCanvas.listReservesMarkerColumn([
      '[4m] ● Client standup',
      '[2h] Ask Adaeze about the design review',
    ])).toBe(true);
    // TASKS/MESSAGES never carry markers — they lose no width.
    expect(HudCanvas.listReservesMarkerColumn([
      '[2m] Adele · Can you send the latest mockups?',
      'Buy filters · Due today 18:00',
    ])).toBe(false);
    expect(HudCanvas.listReservesMarkerColumn([])).toBe(false);
  });

  it('applies the one dense list language to every title', () => {
    expect(HudCanvas.planningFocusRows('MESSAGES', 4)).toHaveLength(4);
    // An eleven-row note now fits ten rows on the first glance; the rest is
    // one swipe away, not three.
    expect(HudCanvas.planningFocusRows('QUICKNOTES · NOTE', 11)).toHaveLength(10);
    expect(HudCanvas.planningFocusRows('QUICKNOTES', 4)).toHaveLength(4);
    expect(HudCanvas.planningFocusRows('QUICKNOTES · RECORDING', 3)).toHaveLength(3);
    expect(
      HudCanvas.planningFocusRows('MESSAGES', 4)[0]?.secondaryBaselineY,
    ).toBeUndefined();
    expect(HudCanvas.usesFlatHudShell('list', 'QUICKNOTES · NOTE')).toBe(true);
  });
});

describe('compact productivity outcome geometry', () => {
  it('uses a compact text stack without a card or progress strip', () => {
    expect(HudCanvas.productivityOutcomeLayout()).toEqual({
      titleBaselineY: 98,
      dividerY: 136.5,
      primaryBaselineY: 158,
      secondaryBaselineY: 181,
      footerBaselineY: 267,
    });
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

  it('marks the selected month week with a slim rail instead of an enclosing box', () => {
    expect(HudCanvas.planningMonthSelectionRail(3)).toEqual({
      x: 28,
      y: 153,
      width: 3,
      height: 22,
    });
  });
});

describe('flat confirmation actions', () => {
  it('does not expose countdown-dependent visual geometry', () => {
    expect(HudCanvas.confirmationReviewLayout()).not.toHaveProperty('progressY');
    expect(HudCanvas.confirmationReviewLayout()).not.toHaveProperty('timerBaselineY');
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
  it('keeps a dense terminal tail on complete speaker lines', () => {
    expect(brainstormTranscriptLines(
      'CODEX > first idea\nCLAUDE > constraint\nHERMES > practical next step',
      42,
      2,
      (value) => value.length,
    )).toEqual(['…CLAUDE > constraint', 'HERMES > practical next step']);
  });

  it('labels terminal recovery states without implying an unrelated tool failure', () => {
    expect(agentTerminalSafetyLabel({ state: 'approval_required', noToolsChecked: false }))
      .toBe('APPROVAL PENDING');
    expect(agentTerminalSafetyLabel({ state: 'unavailable', noToolsChecked: false }))
      .toBe('CONNECTION UNAVAILABLE');
    expect(agentTerminalSafetyLabel({ state: 'blocked', noToolsChecked: false }))
      .toBe('TERMINAL BLOCKED');
    expect(agentTerminalSafetyLabel({ state: 'ready', noToolsChecked: true }))
      .toBe('NO TOOLS CHECKED BEFORE SEND');
  });

  it('makes a running Agent Terminal a transcript-first surface', () => {
    expect(agentTerminalTranscriptLayout({
      state: 'running', noToolsChecked: true, topic: 'hermes · running',
    }))
      .toEqual({
        summary: 'HERMES > RUNNING · NO TOOLS CHECKED BEFORE SEND',
        // Dense: header + LANES in two tight rows, 12 transcript rows.
        dividerY: 78.5,
        transcriptBaselineY: 92,
        transcriptLineCapacity: 12,
        showRecoveryDetails: false,
      });
  });

  it('keeps connection and policy detail on a recoverable terminal state', () => {
    expect(agentTerminalTranscriptLayout({
      state: 'approval_required', noToolsChecked: false, topic: 'hermes · waiting',
    }))
      .toEqual({
        summary: 'HERMES > APPROVAL REQUIRED',
        dividerY: 118.5,
        transcriptBaselineY: 132,
        transcriptLineCapacity: 9,
        showRecoveryDetails: true,
      });
  });

  it('names the lane the wearer is actually focused on', () => {
    // The header was hardcoded HERMES, so a wearer on a calendar lane or a
    // gateway session read someone else's name above their own output.
    expect(agentTerminalTranscriptLayout({
      state: 'running', noToolsChecked: false, topic: 'calendar · running · 2 waiting',
    }).summary).toBe('CALENDAR > RUNNING · TOOL STATE NOT CLEARED');
    expect(agentTerminalFocusLabel('agent:hermes:nightly · running'))
      .toBe('AGENT:HERMES:NIGHTLY');
    // A gateway key we do not choose must not run off the spine.
    expect(agentTerminalFocusLabel(`${'x'.repeat(90)} · running`).length)
      .toBeLessThanOrEqual(24);
    // No lane named yet: the house default, not an empty prefix.
    expect(agentTerminalFocusLabel(undefined)).toBe('HERMES');
    expect(agentTerminalFocusLabel('')).toBe('HERMES');
  });

  it('discloses scroll, terminal handoff and cancel while a session runs', () => {
    expect(brainstormActionHint({
      screen: 'brainstorm',
      state: 'running',
      terminalState: 'ready',
      noToolsChecked: true,
    })).toBe('swipe: scroll · tap: terminal · left x2: cancel');
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
  it('keeps bespoke layouts only for the transcript and the reader', () => {
    // Every other Messages page — thread picker, compose, access, retry —
    // speaks the one dense focus-list language like the rest of the HUD.
    expect(usesMessagesCompactLayout('MESSAGES · CONVERSATION')).toBe(true);
    expect(usesMessagesCompactLayout('MESSAGES · MESSAGE')).toBe(true);
    expect(usesMessagesCompactLayout('MESSAGES')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · COMPOSE')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · ACCESS')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · SETUP')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · RETRY')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · EMPTY')).toBe(false);
    expect(usesMessagesCompactLayout('MESSAGES · READ ONLY')).toBe(false);
    expect(usesMessagesCompactLayout('NOTIFICATIONS')).toBe(false);
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

  it('keeps a compact seven-line transcript free of header and action-card rows', () => {
    const transcript = messagesConversationTranscript([
      'Latest messages — down for older',
      'Dele · newest incoming',
      'You · newest outgoing',
      'Dele · third message',
      'Dele · fourth message',
      'You · fifth message',
      'Dele · sixth message',
      'Dele · seventh message',
      'Say “JOAT, reply saying …”',
      'Mic off? Long press to enable.',
    ], 0);

    expect(transcript.heading).toBe('Latest messages — down for older');
    expect(transcript.rows).toEqual([
      { kind: 'incoming', label: 'Dele', body: 'newest incoming' },
      { kind: 'outgoing', label: 'You', body: 'newest outgoing' },
      { kind: 'incoming', label: 'Dele', body: 'third message' },
      { kind: 'incoming', label: 'Dele', body: 'fourth message' },
      { kind: 'outgoing', label: 'You', body: 'fifth message' },
      { kind: 'incoming', label: 'Dele', body: 'sixth message' },
      { kind: 'incoming', label: 'Dele', body: 'seventh message' },
    ]);
  });

  it('keeps an older selected speaker reachable in the compact transcript window', () => {
    const transcript = messagesConversationTranscript([
      'Latest messages — down for older',
      'Dele · one',
      'You · two',
      'Dele · three',
      'Dele · four',
      'You · five',
      'Dele · six',
      'Dele · seven',
      'Dele · eight',
      'Say “JOAT, reply saying …”',
      'Mic off? Long press to enable.',
    ], 8);

    expect(transcript.rows.map(({ body }) => body)).toEqual([
      'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
    ]);
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

describe('action pill outcomes', () => {
  it('renders transient receipts as a strip and truth states as pages', () => {
    // Wearer's ruling (2026-09-01): a save/delete receipt is a small wide
    // pill like the platform's own listening indicator, not a page takeover.
    for (const phase of ['verifying', 'saved', 'monitoring', 'completed',
      'cancelled', 'not_saved'] as const) {
      expect(HudCanvas.usesActionPillLayout(phase)).toBe(true);
    }
    // failed/check decide whether a retry is safe; due is an interactive
    // alert with an acknowledged delivery. Those must be read, not glanced.
    for (const phase of ['failed', 'check', 'due'] as const) {
      expect(HudCanvas.usesActionPillLayout(phase)).toBe(false);
    }
  });

  it('keeps the strip inside the content field, clear of spine and footer', () => {
    expect(HudCanvas.ACTION_PILL.y).toBeGreaterThan(36);
    expect(HudCanvas.ACTION_PILL.y + HudCanvas.ACTION_PILL.height).toBeLessThan(260);
    expect(HudCanvas.ACTION_PILL.x + HudCanvas.ACTION_PILL.width).toBeLessThanOrEqual(576);
  });
});

describe('home dashboard content', () => {
  it('shows the notifications themselves, never a count', () => {
    // The reference HUDs put the cards on the resting screen; a counter
    // would only tell the wearer to open a page they should not need.
    expect(HudCanvas.parseHudNotificationRows(
      'Calendar · 8m · Design review moved to 4:30\nMail · 23m · Q3 plan is ready',
    )).toEqual([
      { source: 'Calendar', age: '8m', headline: 'Design review moved to 4:30' },
      { source: 'Mail', age: '23m', headline: 'Q3 plan is ready' },
    ]);
  });

  it('keeps a headline that contains the separator intact', () => {
    expect(HudCanvas.parseHudNotificationRows('Mail · 2m · Q3 · final draft')[0])
      .toEqual({ source: 'Mail', age: '2m', headline: 'Q3 · final draft' });
  });

  it('treats an unstructured line as a plain headline', () => {
    expect(HudCanvas.parseHudNotificationRows('Reminder monitoring armed'))
      .toEqual([{ source: '', age: '', headline: 'Reminder monitoring armed' }]);
    expect(HudCanvas.parseHudNotificationRows('')).toEqual([]);
  });

  it('reads the clock and date from the phone, not the Mac', () => {
    expect(HudCanvas.hudClockParts('9:41')).toEqual({ hours: '09', minutes: '41' });
    expect(HudCanvas.hudClockParts('')).toEqual({ hours: '--', minutes: '--' });
    expect(HudCanvas.hudDateLabel(new Date(2026, 8, 1))).toBe('Tue 9/1');
  });
});

describe('navigation map projection', () => {
  const panel = {
    latitude: 41.9147, longitude: -87.6379, zoom: 16,
    width: 248, height: 168, image: {} as CanvasImageSource,
  };

  it('puts the panel centre exactly at the pane centre', () => {
    const bounds = HudCanvas.NAVIGATION_MAP_PANEL;
    const [x, y] = HudCanvas.projectMercatorPanel(
      { latitude: panel.latitude, longitude: panel.longitude }, panel, bounds,
    );
    expect(x).toBeCloseTo(bounds.x + bounds.width / 2, 6);
    expect(y).toBeCloseTo(bounds.y + bounds.height / 2, 6);
  });

  it('places north up and east right, at the Mapbox 512px tile scale', () => {
    const bounds = HudCanvas.NAVIGATION_MAP_PANEL;
    const north = HudCanvas.projectMercatorPanel(
      { latitude: panel.latitude + 0.001, longitude: panel.longitude }, panel, bounds,
    );
    const east = HudCanvas.projectMercatorPanel(
      { latitude: panel.latitude, longitude: panel.longitude + 0.001 }, panel, bounds,
    );
    expect(north[1]).toBeLessThan(bounds.y + bounds.height / 2);
    expect(east[0]).toBeGreaterThan(bounds.x + bounds.width / 2);
    // 512·2^16 px span 360°, so 0.001° of longitude is 93.2px — which is
    // also what makes the 248px pane ≈220m wide. If this drifts, JOAT's
    // route stops sitting on the roads the Mac drew.
    expect(east[0] - (bounds.x + bounds.width / 2)).toBeCloseTo(93.2, 0);
  });
});

describe('short spoken receipts are strips, not cards', () => {
  it('pills a one-line acknowledgement and keeps the card for a real reply', () => {
    // The same wearer ruling applied to Messages and note receipts: these
    // arrive as command answers through the assistant overlay, and a
    // 460x183 card for "Reply sent." is the productivity-page mistake again.
    for (const text of [
      'Reply sent.',
      'Reply cancelled.',
      'New message cancelled.',
      'Saved QuickNote: Ship it today.',
    ]) {
      expect(HudCanvas.usesAssistantPillLayout(text)).toBe(true);
    }
    // A genuine assistant answer still earns the full card.
    expect(HudCanvas.usesAssistantPillLayout(
      'I checked the calendar and reminders. Your design review is next, '
      + 'and reminder monitoring is armed for this evening.',
    )).toBe(false);
    expect(HudCanvas.usesAssistantPillLayout('line one\nline two')).toBe(false);
    expect(HudCanvas.usesAssistantPillLayout('   ')).toBe(false);
  });
});
