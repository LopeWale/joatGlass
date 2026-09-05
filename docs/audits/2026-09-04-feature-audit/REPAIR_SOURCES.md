# Official contracts for the repairs

Accessed September 4, 2026 through Keenable live fetch and independently
reviewed against the installed SDK. External documentation supports the API
and storage assumptions below. It does not prove that JOAT's implementation
works; before/after behavior must supply that evidence.

| Finding | Official source | Applicable contract | Required JOAT evidence |
|---|---|---|---|
| Q1 | [Even Device APIs](https://hub.evenrealities.com/docs/build/device-apis), [Page Lifecycle](https://hub.evenrealities.com/docs/build/page-lifecycle) | Click/double-click/scroll have distinct meanings; one container owns input; a rebuilt page replaces prior state | Actual Back cancels pending discard; stale taps remain blocked; refreshed Save dispatches save; dropped response leaves an escape |
| M1 | [SQLite URI modes](https://www.sqlite.org/uri.html#uriimmutable), [WAL reads](https://www.sqlite.org/wal.html#how_wal_works) | Immutable assumes no other writer and disables change detection; read-only connections can read committed WAL content before checkpoint | Existing and later committed rows visible through public reader methods; DELETE refused |
| M2 | [Even Device APIs](https://hub.evenrealities.com/docs/build/device-apis) | Capture target determines scroll delivery; zero-valued click fields must remain supported | A Mac-authored page produces older/newer requests from real simulator gestures and returns the corresponding content |
| M3 | [Even Page Lifecycle](https://hub.evenrealities.com/docs/build/page-lifecycle), [Device APIs](https://hub.evenrealities.com/docs/build/device-apis) | Page replacement and selection are supported; app owns its domain identity | Back disarms private reply context, reselection opens the displayed thread, late reads cannot restore/clear an obsolete target |
| N3 | [Even Display](https://hub.evenrealities.com/docs/build/display), [Page Lifecycle](https://hub.evenrealities.com/docs/build/page-lifecycle), [Contextual Menu](https://hub.evenrealities.com/docs/build/contextual-menu) | Native list has at most 20 items/64 characters each; image tiles at most 288 by 144; one capture target; serialized image sends; rebuild boolean is authoritative; carry menuObject through rebuilds | Native preferred; rejected native list requires successful visual-page restoration; selected article and Back work; failed restoration stays failed |
| Q2 / transcript portion of Q3 | [Even Display](https://hub.evenrealities.com/docs/build/display), [Device APIs](https://hub.evenrealities.com/docs/build/device-apis) | Bounded list strings and input delivery require application paging; the SDK does not supply a full-note store or reader cursor | Exact source text survives all wire pages, including paragraphs, Unicode and long tokens; actual scroll reaches the last page; Back restores the selected note |
| A1 / M6 | [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling), [Anthropic tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview) | The model requests an operation, application code executes it, and the actual result returns to the model; a schema alone does not perform the action | Authenticated MCP calls reach the existing feature controllers and persistence; message tools return pending state while separate wearer confirmation remains in control |
| M8 sender lifecycle | [Python asyncio subprocesses](https://docs.python.org/3/library/asyncio-subprocess.html#interacting-with-subprocesses) | communicate/wait have no timeout argument; wait_for supplies one; terminate/kill signal the owned child, and communicate drains streams and waits for exit | Exact body reaches argv; timeout/cancellation explicitly stop and reap only the spawned process; post-spawn uncertainty never becomes an automatic retry or delivery claim |
| A2 prompted speech | [Even Device APIs](https://hub.evenrealities.com/docs/build/device-apis), [Even Display](https://hub.evenrealities.com/docs/build/display) | Audio events identify the microphone and contain 16 kHz signed 16-bit mono PCM; image calls must be awaited and checked, but accepted updates are not optical delivery receipts | Preserve event source, establish a prompt-bound audio epoch only after the matching presentation succeeds, reject old/in-flight/mixed provenance, and pass the exact fresh transcript to its typed owner |
| A4 completed-answer latency | [Official Codex event contract](https://github.com/openai/codex/blob/main/sdk/typescript/src/events.ts), [Official SDK response collector](https://github.com/openai/codex/blob/main/sdk/typescript/src/thread.ts) | Turn completion is separate from item completion; the SDK retains the latest completed assistant message | Publish once at turn.completed before process EOF; retain child cleanup, failure handling and safe resume boundaries; [measured repair](CODEX_EOF_LATENCY_REPAIR.md) |

The N3 preferred-native implementation and subsequent source-seam observations
below record the earlier repair stage. The wearer's later typography correction
controls the current UI: News/Search now use the existing compact custom canvas
renderer. [Current visual evidence](READER_ENTRY_VISUAL_RECEIPT.md) records the
accepted scope. A2 source/epoch/presentation seams are now implemented and
composed for typed Messages and Brainstorm; [current runtime gate](PROMPTED_SPEECH_PLAN.md#production-runtime-integration-receipt)
records what passed and which adapters remain unfinished. The official APIs
permit these mechanisms; JOAT owns its prompt identity and lifecycle policies.

Even's pages were revised August 25, 2026. SQLite pages did not supply a
comparable revision date in the fetched content. Installed tools: official
EvenHub simulator 0.9.3 and Even Hub SDK 0.0.14.
The Python subprocess page was opened live on September 4 by root and
independently fetched through Keenable by the sender agent. Its contract
supports the lifecycle mechanism; the chosen application deadline is a JOAT
policy, not a Python or Apple delivery guarantee.

The A2 sources were fetched live again on September 4 while drafting the
speech regressions. The installed SDK declaration describes `speakerRole`
as an app algorithm result, not a firmware identity assertion. Unknown or
absent classification must not make the microphone unusable; explicit other
speaker labels are rejected for prompted input, without claiming biometric
identification. `EventRouter` currently discards the SDK event's actual audio
source when calling `AudioStreamer`, which labels batches from the requested
source instead. That source boundary must be repaired before live prompted
input is accepted.

`HudPresenter.showAssistantOverlay` also returns true when a confirmation
hides the overlay or an existing paint merely schedules another frame.
The prompted-input path therefore needs an awaited result for its own exact
presentation. Reusing that current boolean as prompt visibility would not
meet the intended contract. Even then, simulator pixels and physical optical
timing remain separate evidence from the SDK's call result.

## Boundaries that remain

- [Read-only WAL conditions](https://www.sqlite.org/wal.html#readonly) still
  depend on readable existing sidecars or permission to create them.
  [SQLITE_BUSY](https://www.sqlite.org/wal.html#sometimes_queries_return_sqlite_busy_in_wal_mode)
  remains possible. Read-only is not a guarantee of zero sidecar activity.
- Even does not define JOAT's note actions, message identities, reply consent,
  history cursors or contextual speech ownership. Those require application
  state and integration checks.
- N3 recovery does not explain the native rejection or establish physical
  firmware compatibility. An image-update acknowledgment alone is not proof
  that the wearer can see the resulting page.
  Independent re-review found no proven container-count, capture-target,
  identifier, bounds or row-limit violation in the rejected payload. The
  documented sources do not settle `itemWidth: 0` semantics. The current
  `rebuild=false` receipt also cannot distinguish SDK validation from host
  rejection; the new run retains native debug output for that investigation.
- The [official simulator](https://hub.evenrealities.com/docs/test/simulator)
  supports input and 576 by 288 screenshots but excludes real permissions,
  background lifecycle, BLE timing and hardware performance. Physical-G2
  latency and optical acceptance remain separate.
- The installed official simulator 0.9.3 README, in its v0.6.1 release note,
  documents a hardcoded right-temple eventSource. The comparison runner's
  temporary SDK subscription normalizes double-click to the official left
  enum for contextual Back and records that substitution. Unmodified native
  left-temple delivery is not verified by this fixture.
