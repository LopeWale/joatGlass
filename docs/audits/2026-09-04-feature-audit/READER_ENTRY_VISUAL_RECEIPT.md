# Reader entry visual receipt

**Scoped result: accepted for synthetic Message and text Note readers, with News evidence reused and emoji fidelity unresolved.** Independent review inspected all seven retained retry PNGs individually, then compared the older compact Messages footer. This document records that completed review; no new visual run was performed to write it.

## Run and evidence provenance

Job `job-mtnme82w-6f23605b` completed with exit 0 in 16.743 seconds. The [retry gate receipt](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/reader-entry-retry-receipt.json) reports no change to its 282-file frozen manifest. All production `apps/` entries agree with the first gate; the retry changed the fixture entry convention and used a separate continuation runner. The [journey receipt](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/reader-entry-retry-after/receipt.json) retains 179 copied source hashes before fixture instrumentation, all matching the retry manifest, plus frame hashes and 12 completed simulator gesture receipts.

The first gate's 289 Python tests, mypy on 103 source files, 875 Hub tests and build are **reused checks**, not new retry executions. Its News journey is also reused. The first overall simulator process remains recorded as failed because the fixture's Messages/Notes entry IDs lacked the production `voice-` prefix. Correcting those IDs allowed this retry to exercise the unchanged production entry guard. [First gate receipt](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/exact-readers-gate-receipt.json).

The captured checkout was dirty main at `8386bf5f4a69239c7f7ee84a709cf47701ea242c`; the retained file hashes identify the actual candidate rather than treating that commit alone as its provenance. The acceptance runner SHA256 is `9c1c395c205b0d6ad708f5c2cf14d338d7e354d5e5c0265ac31a0db1c502dfd2`; the simulator binary SHA256 is `e736345ce5d2f7902bb837941ba244e2a03ad182e0a1ae33d70baaa47b49d471`.

## Exact text and gesture evidence

The fixture used synthetic ASCII, CJK, emoji, combining characters, tabs, leading/trailing spaces and CRLF/CR/LF. It exercised the production Mac reader/publisher and Hub presenter/renderer, with synthetic data adapters and a temporary NoteStore. Feature entry was injected; paging, selection and Back used actual simulator inputs with the harness's existing left-Back normalization. No live message, microphone or send was used.

For each row below, the original source, concatenated Hub-received rows and concatenated presenter rows have the same SHA256. Character counts are source-text counts, not rendered glyph or byte counts. The receipt also compares every received page with the Mac wire frame.

| Reader | Evidence run | Characters | Recorded counters | Matching complete-body SHA256 |
| --- | --- | ---: | --- | --- |
| Message | This retry | 980 | 1/3, 2/3, 3/3 | `69137e9d78afc69242f9a4f6ce9237a7734ab2173b309b924fa47c0af6507076` |
| Text Note | This retry | 974 | 1/3, 2/3, 3/3 | `e9858b16b857c648de2fdde6ac8a643274f1b1607b357f50916eb9b7fdc545ab` |
| News | Reused first gate | 974 | 1/3, 2/3, 3/3 | `dfc393e11343c0b464d3b64c993038b9a098885e1ad7937363a3528a144ecb0c` |

The retry's Message and Note receipts report exact wire reconstruction, honest counters, complete footers, final markers, matched forward gestures and restored Back selection. The first/final screenshots independently show the visible endpoints; the middle-page counter and exact whitespace reconstruction are supported by the retained runtime rows. These screenshots do not establish a new reverse-page gesture journey.

## Messages: complete footer, full reader and parent selection

The earlier compact conversation footer visibly ends at `left x2: b`:

![Older compact Messages conversation with truncated Back label](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/compact-messages-after/m2-latest.png)

The retry visibly includes the complete `down older · tap read · say reply · left x2: back` footer. The compact clock, feature label, microphone OFF state and battery remain at the existing banner scale.

![Retry Messages conversation with complete footer](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/reader-entry-retry-after/exact-message-conversation.png)

Page 1/3 shows `MESSAGE BEGIN.`, the intentional initial spacing and blank line, CJK text and subsequent body rows. Page 3/3 shows the end of the unbroken mixed-text value, the final wide row and `FINAL MESSAGE MARKER.`. Body text remains inside the left and right bounds, and the last line does not collide with the footer. The reader footers retain complete Back wording.

![Message first page with BEGIN marker and CJK text](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/reader-entry-retry-after/exact-message-first.png)

![Message final page with final marker and complete footer](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/reader-entry-retry-after/exact-message-last.png)

Back returns to the conversation with the visible selection rail beside the original message row and the complete conversation footer.

![Message Back restores the original message selection](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/reader-entry-retry-after/exact-message-back.png)

## Text Note: full reader and second-row restoration

The Note uses the same compact custom typography and banner. Page 1/3 visibly starts with `NOTE BEGIN.`; page 3/3 includes the final mixed-text row and `FINAL NOTE MARKER.`. CJK and unbroken values stay within the reader bounds, with clear separation between the final line and complete footer.

![Note first page with BEGIN marker and CJK text](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/reader-entry-retry-after/exact-note-first.png)

![Note final page with final marker and complete footer](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/reader-entry-retry-after/exact-note-last.png)

Back restores the second Note row, visibly distinguished from the first different note by its selection rail. The full `tap: detail · left x2: back` footer remains visible. The large age labels come from synthetic old fixture dates.

![Note Back restores the second note selection](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/reader-entry-retry-after/exact-note-back.png)

## Reused News result and remaining limits

The [first News journey receipt](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/exact-readers-after/receipt.json) and its previously inspected first/final/Back images establish full 974-character delivery over three pages, the visible final marker and Back selection on Story 02. That evidence was not rerun or re-reviewed for this document. Earlier apparent missing banner regions were an inspection tool image-delta artifact, resolved by single-image inspection and raw header-pixel comparison; no banner removal defect was found.

Emoji still appear as white circles or silhouettes in the saved images. Exact wire reconstruction does not establish correct glyph appearance. The source-stage hypothesis around color-font rasterization and grayscale conversion remains unproven. A local HTML diagnostic was blocked by the browser tool's security policy and was neither executed nor rehosted; no font or grayscale production change was justified by it.

Search has shared-reader/Panel and Hub renderer test coverage only, with no separate Search simulator visual journey in these gates. Recording-transcript UI, reverse-page visual traversal, real message sending, speech recognition, physical-G2 readability and delivery, assistant latency, packaging and upload are outside this receipt. This acceptance closes the bounded synthetic reader/footer review rather than the overall feature audit.
