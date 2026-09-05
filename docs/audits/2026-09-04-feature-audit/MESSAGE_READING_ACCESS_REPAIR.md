# Messages group reading and access recovery

The next repair addresses M4 and the startup ownership part of M10. Group threads must open the same selectable conversation and exact-text reader as direct conversations, with older/newer pages and Back retained. Reading a group must never arm a reply to its latest participant. Incoming rows need their actual sender; group titles are not participant names. Reply instructions must disappear where replying is unsupported.

A missing database at boot must not permanently remove the Messages reader, assistant tool or notification lane. Keep each service registered, handle each access failure honestly, and retry using the same store. Do not claim macOS permission changes always become effective without restarting the process.

Plan: reproduce with synthetic SQLite and production controllers; add participant labels; separate reading from reply ownership; remove startup availability filtering; verify identical scenarios and related Messages/navigation/notification tests. Simulator and physical G2 acceptance remain separate, pending manual Mac unlock. No live Messages data, sends, permission changes, package or upload.

Official references checked 2026-09-05 UTC:

- [Apple Privacy & Security](https://support.apple.com/guide/mac-help/change-privacy-security-settings-on-mac-mchl211c911f/mac) documents Full Disk Access covering data from Messages. Quote: “including data from other apps”. This supports the permission guidance, not an official chat.db schema or hot-permission-reload guarantee.
- [SQLite URI filenames](https://www.sqlite.org/uri.html) documents opening a database with `mode=ro`. Quote: “the database can be opened read-only”. Retain WAL-aware read-only connections. The per-message handle relation is an existing JOAT adapter assumption verified against synthetic schema fixtures, not represented as Apple's published database contract.

Remaining outside this slice: attributedBody-only messages, conversation-picker coverage beyond the existing query window, group sending, relationship/contact disambiguation, draft editing and live STT/simulator acceptance.

## Before and focused after evidence

`evidence/remediation/message-access-red.log`: the seven-case initial contract run had six failures and one pass. Two group-opening checks stayed on the picker instead of reaching history; the participant field was absent; the model tool propagated unavailable/access errors. The already-retained store recovered in the standalone router even before this change, confirming that M10's missing dependency at production construction was the seam to fix, not a broken retry in the router itself.

`message-access-first-after.log`: all seven initial cases pass. The group reader preserves the complete synthetic body, including repeated spaces/newlines, through the existing wire serializer and page controls; it reaches older entries, returns to newer entries, and clears history on Back.

`message-access-ownership-after.log`: ten checks pass, including three real MessageUI/MessageReplyController selection races. A group selection immediately disarms a prior direct reply; while its read is held, neither bare Reply nor inline reply opens a prompt/confirmation. A released stale group read cannot replace a newer direct thread or Home. All send sinks are synthetic and remain unused.

`message-access-unit-after.log`: 122 related store/controller/tool tests passed in 3.78 seconds. Strict mypy passed on 107 production files. Later additions cover unknown group participants, recovery after a permission exception, and a registered notification lane whose database appears later.

`message-access-recovery-after.log`: the notification case successfully initialized after a missing-database failure, skipped preexisting history, ingested the new row, and advanced its durable cursor. Its last assertion incorrectly compared the stored JSON bytes with a string, yielding one test failure and nine passes. The assertion now decodes the existing JSON storage format; the combined gate will verify that corrected test. No production change was made for this test error.

Production `__main__.py` now retains `imessage` in the router, registers the model read tool unconditionally within the existing model branch, and always adds its notification lane when notifications are enabled. This is source wiring evidence; the full engine and an actual macOS permission grant have not been exercised.

The frozen combined gate includes the new tests, existing Messages prompt and action ownership integration, read-only/WAL checks, navigation and rendering tests, strict types, and production build. Pending receipt: `evidence/remediation/message-reading-access-gate-receipt.json`.

## Accepted combined host gate

CPJ `job-mtnuodzi-3c79c733` completed exit 0: 726 Python tests, 333 Hub tests across seven suites, strict mypy on 107 production files, and production Hub build. The corrected notification recovery test passed. All 298 frozen files still matched at root's post-completion inspection. Receipt SHA-256: `28927f38d0c539e96e498d24e97b54bf81319f8e8c6b667e0b8f9779090ae577`. Simulator, full engine launch, real permission-grant behavior and physical G2 acceptance remain pending.
