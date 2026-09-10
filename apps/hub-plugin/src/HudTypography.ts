/**
 * The HUD's one type system. Every canvas font in the plugin resolves to a
 * role here; no draw site invents its own number any more (the 2026-09-05
 * audit found 18 fixed sizes, two families and eleven leadings doing the
 * same handful of jobs).
 *
 * Two families, per the wearer's ruling (see the note that used to live on
 * HUD_TEXT_FONT): official G2 screens set light PROPORTIONAL type — mono at
 * small sizes reads as a bold slab on the phosphor display and fits ~74
 * characters where the proportional face fits ~95. So all reading text is
 * PROSE at weight 400 (canvas default weight renders these families heavier
 * on some hosts). MONO remains only where column alignment or tabular
 * numerals are the point: the status spine, clocks, countdowns, dates, and
 * the reader body — which draws the Mac's prepared 62-column rows verbatim
 * and is pinned to a monospace grid by that wire contract
 * (services/reader.py _ROW_COLUMNS).
 */

export const MONO_FAMILY = '"SF Mono", SFMono-Regular, Menlo, monospace';
export const PROSE_FAMILY = '"Helvetica Neue", "SF Pro Text", Arial, sans-serif';

export const mono = (px: number): string => `${px}px ${MONO_FAMILY}`;
export const prose = (px: number): string => `400 ${px}px ${PROSE_FAMILY}`;
/** Assistant turns keep JOAT's approved proportional family while gaining
 * the restrained emphasis the Even AI transcript gives its answers. */
export const assistantProse = (px: number): string => `500 ${px}px ${PROSE_FAMILY}`;

/**
 * The roles. One job, one font. Sizes are the fewest that preserve the
 * existing hierarchy: 24/19 for at-distance navigation, 16 for what a page
 * is about, 14 for what it says, 11/10/9 for everything that annotates.
 */
export const TYPE = {
  /** Page header — what page this is ('Planning', 'Calendar review').
   * 15, not larger: the header band shares y56-64 with dividers on every
   * page, and the G2 aesthetic is compact — a header never outweighs the
   * primary statement below it. */
  title: prose(15),
  /** The page's leading statement (nav primary, outcome title). */
  primary: prose(16),
  /** Active-guidance instruction: read while moving, one notch larger. */
  guide: prose(19),
  /** At-distance alert primary (rerouting, warnings). */
  glance: prose(24),
  /** Reading body and pills. */
  body: prose(14),
  /** Assistant answer inside the retained conversation transcript. */
  assistant: assistantProse(14),
  /** A selectable row (focus lists, conversations, day entries). */
  row: prose(14),
  /** Secondary line under a primary — on STATIC pages (decision, preview,
   * arrival, error). Screens read while MOVING (active guidance, warnings)
   * set their secondary in `body` instead: one deliberate step up, same
   * face, so the rule is two roles, not a per-screen number. */
  secondary: prose(11),
  /** Brainstorm/terminal transcript body. HUD_TEXT_MEAN_ADVANCE_PX and
   * BRAINSTORM_ROW_COLUMNS (105) are measured against exactly this. */
  transcript: prose(11),
  /** Metadata: sources, ages, counters, in-content hints. */
  meta: prose(10),
  /** Uppercase section label / eyebrow. */
  label: prose(10),
  /** THE footer hint. One face, one size, centered at HINT_BASELINE_Y. */
  hint: prose(10),
  /** Dense annotations: chips, speaker tags, overflow counts. */
  micro: prose(9),
  /** Dense NUMERIC annotations that must column-align (time chips,
   * calendar markers, week counters). */
  microMono: mono(9),
  /** Status spine clock. */
  statusClock: mono(12),
  /** Status spine label, mic and battery words. */
  statusMeta: mono(10),
  /** Reader body: draws the Mac's prepared 62-mono-column rows verbatim;
   * stays monospace until core/protocol.py's column model changes with it
   * (they must move in the same commit, like every wire shape). */
  readerBody: mono(14),
} as const;

/** Leadings, also by job — not one per draw site. */
export const LEAD = {
  /** 16px primary/title wraps. */
  primary: 20,
  /** 14px body wraps. */
  body: 18,
  /** 14px body in dense panes (2-line clamps inside 36px slots). */
  tight: 16,
  /** 10px meta wraps. */
  meta: 14,
  /** 11px transcript rows (was a magic 14 inside the draw loop). */
  transcript: 14,
  /** Reader rows: 10 rows of 14px mono between y84 and the footer. */
  reader: 17,
} as const;

/**
 * Row pitches: the vertical step from one row's baseline to the next.
 * Distinct from LEAD (the step between WRAPPED lines of one row). Values are
 * unchanged from the surfaces they came from — naming them is the point, so
 * a pitch can never again be a bare number nobody can trace.
 */
export const PITCH = {
  /** Dense selectable rows: focus lists, pomodoro controls. */
  row: 20,
  /** Rows carrying a speaker/label column above the body: conversations. */
  labelled: 22,
  /** A calendar week band. */
  week: 22,
  /** Boxed choices with a border and padding: navigation decisions. */
  boxed: 34,
  /** A month-grid band on the full-page planning calendar. */
  monthBand: 26,
  /** An entry row in the planning dashboard's day pane. */
  paneRow: 24,
} as const;

/** Every footer hint sits on this baseline, middle-aligned, centered. */
export const HINT_BASELINE_Y = 267;

/** The 3px selection rail is this tall beside every selectable row. */
export const RAIL_HEIGHT = 19;

/** The one draw-time case transform. Labels and eyebrows uppercase through
 * here so the discipline is greppable; matchers that only DETECT case keep
 * their own toUpperCase. */
export const upper = (value: string): string => value.toUpperCase();
