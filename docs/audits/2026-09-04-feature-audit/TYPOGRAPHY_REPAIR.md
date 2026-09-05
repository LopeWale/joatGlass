# Typography and reader repair: wearer correction

The wearer rejected the oversized native News and JOAT MIC OFF lettering. The selected direction is the existing custom JOAT font and compact banner, consistently across News, Messages and readers. The proposed native migration was based on a misread reference and has been stopped; its experimental files/tests are removed without reverting earlier functional work.

## Verified cause and provenance

- Commit 6ff0faf (September 2) moved the changing banner word from canvas to native SpineLabel to avoid image transfers.
- Commit d89d0b0 (September 3) moved News/Search to native listLayout for firmware scrolling. This replaced the authored banner and typography.
- Both paths were active in the primary checkout when the wearer reported the mismatch; the repair removes their use from the production custom surface. Previous after receipts copied working source; they were not proof of design acceptance. Design QA from August 27 is historical, not a substitute for current visual verification.
- The reported reader wrapped at 52 columns and retained 36px side margins. The unused right area was not a reserved feature pane. The repaired reader uses 62 conservative columns with a 14px left edge, or 36px where the News/Search tab rail is present.

## Bounded repair and acceptance

1. Restore the compact canvas label using the existing font; avoid duplicate MIC OFF in the title and right-side indicator.
2. Keep News/Search on the same custom surface through open, scroll, article and Back, preserving selected-row/action identity.
3. Expand reader geometry and safe wire rows without truncating source text or hiding page content.
4. Capture current-source simulator News, Messages and full Note pages. Inspect font, header scale, text width, footers, selected rows and page reconstruction. Report image-transfer cost honestly.

Even native text has no font family or size controls. Four serialized 288x144 image containers fit the 576x288 display. These constraints support using the existing raster renderer; they do not prove physical-glasses latency or typography.
[Official Even display documentation](https://hub.evenrealities.com/docs/build/display), fetched live September 4, 2026.

Status: the fresh current-source simulator run confirms compact typography, News selection and wider Notes. The same Note now occupies 488px rather than 393px and takes two pages rather than three. Full visual acceptance failed because Messages footer text was cut off; the targeted footer and exact-reader follow-up is underway. [Measured visual receipt](COMPACT_UI_VISUAL_RECEIPT.md). Earlier native-experiment test logs are abandoned-direction evidence, not acceptance of this repair.

## Adversarial pass

The fresh review caught a new overlap between the expanded article text and the x12..26 tab rail. Two regression cases failed before correction; rail-bearing articles retain a 36px left edge and use x562 on the right. Notes and Messages use x14..562. All 137 renderer/builder tests and 182 focused presenter/menu tests pass after correction.

A separate existing finding remains open for simulator acceptance: Messages/News/Search used character-count pages that lost wide Unicode text through clipping or row overflow. The subsequent shared exact-reader fix has focused source/test evidence; fresh mixed-text simulator proof is pending. The compact ASCII captures establish typography, banner, selection and exact Note-width changes only.

Restoring custom lettering changes the transfer cost: a changing banner label can dirty the top-left image tile; custom News selection also needs changed tiles. The existing coalescing, unchanged-tile skipping and serialized pacing remain. These changes are not a measured assistant-latency improvement.
