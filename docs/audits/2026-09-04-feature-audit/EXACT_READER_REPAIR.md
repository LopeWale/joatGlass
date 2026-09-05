# Exact reader and complete footer repair

The bounded requirement is to show the full source text when opening a Message, News article, Search result or Note, using the existing compact JOAT UI. Pages must remain reachable in both directions; Back must return to the correct parent. This follows the wearer's typography correction and the observed clipped Messages footer.

## Observed defect and cause

The [production-wire probe](evidence/remediation/unicode-reader-before.json) reproduced 440 unbroken ASCII or CJK characters reduced to 60; repeated emoji and original whitespace also failed exact reconstruction. `panels._chunks` shortened every long word to its first 60 characters, while character-count pages and a second Hub word-wrap could overflow the ten visible lines. This is reproduced source-to-wire loss, not a hypothesis based on screenshots.

The Messages footer was independently cut at 46 characters in both Mac serialization and Hub list normalization. The [compact visual receipt](COMPACT_UI_VISUAL_RECEIPT.md) records the screenshot failure despite a successful process exit.

## Implementation

- A shared Mac paginator emits exact source spans: at most ten body rows per page, 62 conservative display columns per row and 64 UTF-16 units including retained CRLF. Wide or astral characters cost two columns; tabs cost eight. Ordinary combining, joined emoji and flag groups stay together where they fit. A group exceeding the hard row budget is split at code-point boundaries without deleting source text.
- Messages, Notes, News and Search use those prepared rows. Concatenating them reconstructs the exact original source. Page-counter-first metadata identifies the prepared contract; empty article metadata gets a readable fallback label.
- The Hub paints prepared rows directly. It interprets line terminators and tabs for display, and preserves spaces, blank lines and complete unbroken values. It does not trim, join, wrap or add body ellipses. An invalid overfull prepared page fails rendering explicitly. Older Mac wire shapes retain their compatibility path.
- Footers allow 96 characters before the existing compact rendering. Feature headings retain their separate 46-character cap. Full readers keep the newly expanded right edge; News/Search leave room for their visible left tab rail.

## Evidence and scoped acceptance

[Mac red](evidence/remediation/exact-reader-mac-red.log): 41 failures and one pass. Nineteen failures were new shared-helper contract tests against the deliberately empty implementation; the remaining failures reproduced producer and wire behavior. [Mac after](evidence/remediation/exact-reader-mac-after.log): 285 passed across readers, capabilities, panels, news, search and protocol.

[Hub red](evidence/remediation/exact-reader-hub-red.log): six failed scenarios across the full-reader families and overfull-page rejection. [Hub after](evidence/remediation/exact-reader-hub-after.log): 105 passed; TypeScript also passed. [Footer after](evidence/remediation/compact-footer-hub-after-corrected-fixture.log): all 74 Presenter tests pass, following removal of an erroneous test cleanup call.

Fresh adversarial review caught a bare page counter when publisher metadata was empty, which would select the older rendering path. [Four regression failures](evidence/remediation/exact-reader-metadata-red.log) drove a metadata-only fix; [all 30 Panel tests pass](evidence/remediation/exact-reader-metadata-after.log). A separate source review found no additional material issue in the prepared-row/Hub boundary, rail geometry or navigation guards.

The first frozen-candidate gate passed 289 Python tests, mypy on 103 source files, 875 Hub tests and build with unchanged source. Its simulator process failed: injected Messages/Notes entry IDs lacked the production `voice-` prefix and were correctly rejected before interaction. The fixture was corrected to use the validated convention; no product entry rule was relaxed. [First gate receipt](evidence/remediation/exact-readers-gate-receipt.json).

News delivered all 974 original mixed-text characters across three honest pages; received/surfaced/source SHA256 agree, all footer controls survive, the final marker is visible, and Back selects Story 02. Root inspected all three PNGs. Separate single-image review and raw header-pixel comparison confirm the banner remains visible; an initial apparent disappearance was the inspection tool's unchanged-image-region suppression, not a product defect. Some emoji appear as solid circles or silhouettes, so glyph fidelity is **unverified** despite exact wire content. An HTML color-to-gray diagnostic was prepared but its local URL was blocked by the browser tool's security policy; it was not run or rehosted. No speculative production conversion or font change was made.

The Messages/Notes retry, job `job-mtnme82w-6f23605b`, completed with exit 0 in 16.743 seconds. Its 282-file manifest remained unchanged during the run, and all production entries match the first gate. It reused that gate's Python, mypy, Hub and build checks, and the already accepted News journey. The retry did not rerun News or those checks. [Retry gate receipt](evidence/remediation/reader-entry-retry-receipt.json).

The 980-character Message and 974-character Note each reached the Hub intact across counters 1/3, 2/3 and 3/3. Source, received-row and presenter-row SHA256 agree for each complete body, and actual simulator gesture receipts record forward paging and Back. Independent inspection of all seven retry PNGs confirms the compact banner/font, visible BEGIN and FINAL markers, CJK and unbroken rows inside the reader bounds, complete Back copy, and restored parent selection. The conversation footer now fully reads `down older · tap read · say reply · left x2: back`, where the older compact capture ended at `left x2: b`. [Exact hashes, image comparisons and scoped visual acceptance](READER_ENTRY_VISUAL_RECEIPT.md).

This closes the synthetic Message/Note reader and footer visual gate, with the first-gate News acceptance reused. Emoji silhouettes remain unresolved. Search retains shared-reader/Panel and Hub renderer test coverage only; no separate Search simulator visual journey was performed. The new screenshots cover forward page traversal and Back to the parent, not a fresh reverse-page gesture journey or recording-transcript reader acceptance.

## Official basis

[Even's display documentation](https://hub.evenrealities.com/docs/build/display) specifies the 576x288 display, native list limits of 20 items and 64 characters, and the native font's lack of font or size selection. The custom image path preserves the user's existing typography within four 288x144 image containers. The stricter UTF-16 and display-column limits above are JOAT's conservative transport/layout decisions; they are not claims that Even prescribes this paginator. [Live official-source snapshot](evidence/remediation/official-even-display-2026-09-04.md).

This change does not establish message sending, speech transcription, physical-G2 delivery, assistant latency, deployment or acceptance of the remaining audit findings.
