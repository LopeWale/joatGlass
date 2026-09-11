# Navigation, Messages and Planning: visual evaluation of release 0.1.84

Evaluated September 10, 2026, after committing and uploading the assistant-conversation changes. This report evaluates the existing product; it does not change the requirements or implement feature fixes.

## Release receipt

Implementation commit `daafac8`; release metadata commit `ca0c213`; private Even Hub build **0.1.84**. The exact package is 140,525 bytes, SHA-256 `6f063cc1996fdc55c846eca19b88e0423b214998cb53b52f54940163feee9679`. After Add build and browser reload, the authenticated JOAT private-build list displayed v0.1.84 above v0.1.83. The optional portal changelog was not entered. No push, public publication, physical install, or Mac app deployment is claimed.

Portal: https://hub.evenrealities.com/hub/com.akinwale.joat

## Evidence and method

The official Even simulator 0.9.3 rendered a snapshot of the release Hub source with SDK 0.0.14. All 83 snapshot source files match the checkout. The observer used production HubGatewayTransport, CapabilityRouter, IMessageStore, MessageReplyController, NavigationSession and ProductivityActionController. Its database, contact lookup, final message sender, routing search and EventKit-shaped store were disposable fixtures. All real sends and native data writes were excluded by construction.

89 named journey captures and nine distinct transition captures were retained. Screenshots store optical intensity in alpha; review the alpha previews or decode the original alpha channel. Command elapsed times include deliberate waits and are not latency measurements. Some original frame labels describe intended states that did not occur; the verified descriptions below supersede those labels.

No automated test-suite result was used as visual acceptance. Independent framing, symptom, public-evidence, local-process and challenge reviews cross-checked the findings. Final rendered frames and intermediate transitions were examined separately.

## Findings

### N1 — Navigation briefly presents contradictory states across display quadrants

**Confirmed in the simulator framebuffer, reproduced.** The new error says “Phone location unavailable” and “Allow Location for Even Hub, then retry,” while old lower quadrants retain “Getting your location,” “Keep phone foreground and unlocked” and the rounded locating shell.

The repeated capture compares each 288×144 original-alpha quadrant against the complete locating frame and complete error frame:

| Milliseconds after the observer issued mode confirmation | Top left | Top right | Bottom left | Bottom right |
| --- | --- | --- | --- | --- |
|900|New|Old|Old|Old|
|1030|New|New|Old|Old|
|1116|New|New|New|Old|
|1240|New|New|New|New|

Mixed snapshots span 216ms. This is an observed span between sampled frames, not the exact transition duration and not physical-glasses latency. Every intermediate quadrant matches its corresponding complete old/new reference pixel for pixel. Independent decoding reproduced the classification.

Local mechanism: `HudCanvasRenderer.ts:1859` rasterizes the full canvas before splitting it. `HudVisualSurface.ts:335` defines four quadrants; `:527` forces full transfer for Navigation; `:535` uploads them sequentially and awaits each SDK update, with pacing. Its complete-frame acknowledgement happens after the loop (`:597`). This protects accepted interaction state but does not make intermediate display composition atomic. `NavigationController.ts:118` waits for the complete paint before accepting the new view. Mac `services/navigation.py:548` and `:642` issue separate locating/error states.

The framebuffer sequence and local transfer path strongly support this mechanism. A synchronized native-window recording was not obtained: Computer could not identify the CLI simulator as a controllable native app. Screenshot readback versus the exact window refresh remains a boundary; physical G2 occurrence remains unproved.

### P1 — Planning loses current-date and page context after a completed action

**Confirmed presentation difference and missing context.** Opening Planning shows the split month/day view with Planning, SEP 2026 and TUE SEP 1. After fixture Calendar creation, Reminder creation, and Reminder completion, return shows a full-width day list. It includes previous/next-day and VIEW MONTH/WEEK/DAY controls among the entries but omits the current date and Planning title. The created item appears and completed Reminder disappears correctly; this is not a failed-save or stale-data allegation.

Local explanation: Mac `capabilities.py:1841` returns anchored writes to DAY scope; grid data is supplied only for MONTH (`:1590`). `CapabilityMenu.ts:634` creates the split dashboard only with a 42-cell month grid. `HudCanvasRenderer.ts:2730` draws the day list rows/footer without drawing `frame.title`.

Returning to the relevant day is documented behavior; this report does not declare the day scope itself wrong. The visible loss of date/context is the UX concern.

### M1 — Messages changes reading typography and drops conversation identity

**Confirmed visual differences; design acceptance unresolved.** Conversation text is proportional. The full reader switches to a heavier-looking fixed-width face. `HudTypography.ts` explicitly provides proportional prose and mono readerBody; the latter follows the Mac's prepared column contract. This establishes the implementation reason, not proof that the user approved that visual difference.

The transcript uses a generic heading such as “Latest messages — down for older”; the reader shows page/direction/read metadata without the contact/group title. Fixture sender identifiers truncate in a small column; that alone cannot establish real-contact legibility. Confirmation correctly names Adaeze and displays the complete draft, so there is no wrong-recipient or accidental-send finding.

The current DESIGN.md and HUD_DASHBOARD.md contain older font/menu/Planning descriptions. Source comments claiming wearer approval were not treated as independent approval evidence. This evaluation records the visible differences without silently rewriting the design spec.

## Interactions that worked in the observed cases

- Native SDK menu opened over feature pages, selected Messages/Planning/Navigation, scrolled to Settings, and dismissed back to the exact prior reader pixels (frames 57 and60 hashes identical).
- Messages selected one-to-one and group conversations; showed unread/attachment labels; displayed an explicit recipient/draft confirmation; refused the fixture reply when cancelled; marked the group read-only and did not arm its reply target.
- A 2,375-character message with numbered segments 01–40 opened over five reader pages, showed the final END OF MESSAGE marker, and returned to page 1. Older history was reached and Up restored the latest page (frames80–89).
- Planning moved from September 10 to September 1, showed an Event and a Reminder in the same pane, selected an item and opened detail.
- Calendar creation showed review, accepted confirmation, visible SAVED receipt, and a returned agenda containing the created event. Reminder creation showed SAVED then MONITORING and appeared in the returned agenda. Completion showed its outcome and removed that Reminder. These were fixture controller operations.
- Navigation selected destination/travel mode, changed Walk to Cycle, reached truthful simulator location-unavailable handling, retried and cancelled to Overview. The location failure itself is not a diagnosed phone or pairing defect.

## Rejected evidence and remaining boundaries

- The old productivity driver stopped before feature evaluation: its startup hook now opens Planning directly, while its first assertion expects a retired custom menu cursor at x 10–16. Exact failure: `capability menu cursor did not render on row 0; content-free occupancy=(0.0, 0.0)`. This is a locally verified driver mismatch, not proof that Planning failed. The driver rejected, rather than silently passing, this mismatch.
- A layout-only Navigation capture attempt produced identical destination-confirmation pixels under all eleven requested state names. Those captures are rejected as evidence of preview/guidance/rerouting/arrival. The related bridge-log driver also failed its acknowledgement wait. No active-route claim is made.
- An initially suspected missing status strip was withdrawn after original-alpha comparison proved the strip remained present; only clock pixels changed.
- Physical temple-side distinctions, actual G2 optical behavior, real voice/model timing, native EventKit permissions, live store readback and real Messages delivery were not verified. Simulator menu/fixture evidence cannot establish them.
- New-message compose, provider-success guidance, undated/large-volume Planning, date-boundary behavior and assistant interruption over each feature were not fully exercised in this run. No broad “all features passed” claim is warranted.

## Evidence-led Five Whys disposition

The concrete active Why was: why can a transition display new error quadrants with old locating quadrants? Local source and repeated original pixels establish the bounded mechanism above. Public sources corroborate individual-container updates and serial sending, but all applicable SDK/platform sources originate from Even Realities. The required three independent-source gate therefore returns **SOURCE_GATE_FAIL**. The chain stops at the locally supported mechanism; no fabricated five-link explanation or deeper organizational cause is admitted.

Relevant primary sources, accessed 2026-09-10:
- [Official contextual-menu contract](https://hub.evenrealities.com/docs/build/contextual-menu): OS ownership, page declaration, selection/foreground sequence and simulator controls.
- [Even Hub testing boundaries](https://hub.evenrealities.com/docs/test): simulator/package/physical evidence differ.
- [Even SDK reference](https://github.com/even-realities/everything-evenhub/blob/main/plugins/everything-evenhub/skills/sdk-reference/SKILL.md): per-container serial image sending; document declares an older SDK version and is not independent corroboration.
- [Published SDK 0.0.14](https://registry.npmjs.org/@evenrealities/even_hub_sdk/-/even_hub_sdk-0.0.14.tgz): current ImageRawDataUpdate targets a single container; no public whole-frame commit field in that type.

**One recommended intervention:** make Navigation transitions visually coherent throughout the required serial tile transfer, treating intermediate frames as part of the user interface. Preserve supported SDK pacing; do not assume parallel sending is safe. No feature fix was implemented in this evaluation.

**One seven-day signal:** proportion of recorded loading→outcome Navigation transitions containing contradictory old/new instructions. Retain the denominator and original captures; target zero across the observed set.

## Retained evidence

[Source identity receipt](/Users/emmanuelakinwale/.codex/visualizations/2026/09/10/01a088f4-ef68-7ac2-bf90-225f5ce0a8d5/feature-evaluation-0.1.84/source-receipt.json)

[Initial contradictory Navigation frame](/Users/emmanuelakinwale/.codex/visualizations/2026/09/10/01a088f4-ef68-7ac2-bf90-225f5ce0a8d5/feature-evaluation-0.1.84/journeys/36-navigation-location.alpha.png)

[Repeated transition, new top / old bottom](/Users/emmanuelakinwale/.codex/visualizations/2026/09/10/01a088f4-ef68-7ac2-bf90-225f5ce0a8d5/feature-evaluation-0.1.84/navigation-transition/01030.alpha.png)

[Timestamped quadrant comparison](/Users/emmanuelakinwale/.codex/visualizations/2026/09/10/01a088f4-ef68-7ac2-bf90-225f5ce0a8d5/feature-evaluation-0.1.84/navigation-transition/tile-comparison.json)

[Planning before operation](/Users/emmanuelakinwale/.codex/visualizations/2026/09/10/01a088f4-ef68-7ac2-bf90-225f5ce0a8d5/feature-evaluation-0.1.84/journeys/19-planning-sept1.alpha.png) · [Planning after saved operation](/Users/emmanuelakinwale/.codex/visualizations/2026/09/10/01a088f4-ef68-7ac2-bf90-225f5ce0a8d5/feature-evaluation-0.1.84/journeys/29-calendar-return.alpha.png)

[Messages transcript](/Users/emmanuelakinwale/.codex/visualizations/2026/09/10/01a088f4-ef68-7ac2-bf90-225f5ce0a8d5/feature-evaluation-0.1.84/journeys/11-dele-thread.alpha.png) · [Full reader](/Users/emmanuelakinwale/.codex/visualizations/2026/09/10/01a088f4-ef68-7ac2-bf90-225f5ce0a8d5/feature-evaluation-0.1.84/journeys/52-long-reader-first.alpha.png)

[Recorded journey observations](/Users/emmanuelakinwale/.codex/visualizations/2026/09/10/01a088f4-ef68-7ac2-bf90-225f5ce0a8d5/feature-evaluation-0.1.84/journeys/observations.json)
