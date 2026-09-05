# News Back selection: verified simulator improvement

**Scope correction after wearer review:** this receipt proves selection only.
The initial News picker uses large native lettering; the restored picker and
Messages screens use much smaller, heavier raster lettering. That visible
inconsistency and the clipped Messages footer were not resolved by the
selection repair. Cross-feature visual acceptance is **not met**.

Same synthetic stories and gesture sequence: open News, swipe down, tap Story
02, Back, tap again. The initial list and article are readable in both runs.

| Observation | Before | After |
|---|---|---|
| Visible row selected after Back | Story 01 | Story 02 |
| Article opened by the next tap | Story 02 | Story 02 |
| Visible selection matches next action | No | Yes |

![Before: Story 01 outlined while next tap opens Story 02](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/wave2-visual-news-after/n3-back.png)

![After: Story 02 visibly selected and reopened by the next tap](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/prompt-news-after/n3-back.png)

The repair waits for an actual selected-row paint before enabling input.
Programmatic nonzero restoration uses the existing canvas list, so it is
denser than the initial native list. That earlier functional repair used this
workaround because Even exposes native scroll highlighting but no programmatic
scroll-position setter. The wearer subsequently rejected the visual mismatch;
the current typography repair supersedes that mixed-style direction.
[Official Device APIs](https://hub.evenrealities.com/docs/build/device-apis),
[Display contract](https://hub.evenrealities.com/docs/build/display).

Both root and an independent agent inspected all new images individually.
The initial-list file is nonblank. Root's image-tool presentation artifact
does not constitute a blank-screen finding.

The frozen candidate passed 506 Python tests, strict mypy for 102 source files,
728 Hub tests, build, and the fresh simulator journey. No frozen source changed
during the run. Retained receipts: `evidence/remediation/prompt-news-receipt.json`,
`prompt-news-after/receipt.json`, and `prompt-news-visual-review.json`.
The two new prompt-transport test files were authored outside that gate and
are not included in these passing counts.

This proves the isolated simulator Back/reopen behavior. Native rejection
causes, longer multi-feature sequences, physical G2 behavior and live prompted
microphone input remain separate acceptance work. No message was sent and no
package was uploaded. Runner durations include startup/waits and are not
assistant-latency measurements.
