# Engineering Rules

Non-negotiable rules for this codebase. PRs violating these get rejected.

## Code organization (OOP)

1. **Program to interfaces.** Every external dependency (BLE, STT, CLIs, channels, computer-use) sits behind an ABC in `joat/core`. Concrete adapters live in their own package. No orchestrator code imports a vendor library directly.
2. **One class, one responsibility.** Transport doesn't parse audio; audio doesn't render HUD pages; agents don't verify their own citations.
3. **Dataclasses for domain models**, typed end-to-end (`mypy --strict` clean). Events over direct calls: components communicate via the `EventBus`.
4. **Vendor code stays in `vendor/`** — never copy third-party source into `apps/`. Wrap it with an adapter instead. `vendor/` is gitignored; `scripts/clone_vendors.sh` reproduces it.
5. **Tests required**: unit tests for every core class; integration tests for transport auth, verifier pipeline, and the debate state machine.

## Safety & consent

6. **Every consequential computer-use action requires explicit confirmation** (IMU nod or R1 ring). No exceptions, even in demos. Sandbox Mac actions in a Lume VM.
7. **Consent tones for call recording.** Follow local recording-consent law.
8. **Local-first privacy:** transcripts and notes stay on the Mac unless a task explicitly needs cloud. Log content hashes, not content, in audit trails.

## ToS & licensing

9. **Claude critic:** invoke only the official `claude` binary (`claude -p`). Never reuse Claude OAuth tokens in any other client, SDK, or third-party tool. Keep the loop switchable to a metered API key with `--max-budget-usd`.
10. **Codex:** ChatGPT-subscription auth in Codex CLI is permitted — fine as-is.
11. **Firmware:** stock G2 firmware only (ECDSA-P256-signed images). App-layer BLE protocol only. Do not depend on `g2flash`.
12. **Licenses:** BlackHole is GPL-3.0 — use as an installed driver, don't link/vendor its code into ours. Check each cloned repo's license before deriving code from it.

## Debate loop

13. **No submission passes without verification.** Every non-trivial claim ≥1 citation; every citation carries a verbatim quote. Unverifiable ⇒ DENY + structured repair request. Cap repair retries and total rounds.
14. **Judge threshold:** convergence requires agreement ≥ 0.85 and zero blocking issues.
15. **Re-verify CLI flags before automation.** Codex and Claude CLIs move fast — check `--help` against the pinned version in `config/` before wiring flags into the loop.

## Hardware constraints (design against these)

16. HUD canvas = **576×288 monochrome green**. All display code targets this; nothing else.
17. **One BLE central per lens.** Handoff is always: disconnect → rescan → reconnect both lenses → replay 7-packet auth + init → resend page state.
18. Even Hub relay: WebSocket domains must be exact-matched in `app.json` (no wildcards) or the Hub silently blocks them.
19. **Direct-BLE path only:** mic enable is `[0x0E, 0x01]`; LC3 frames arrive 0xF1-prefixed on NUS; decode via `liblc3` to 16 kHz mono 16-bit PCM. On the **Hub relay path the SDK already delivers 16 kHz mono 16-bit LE PCM** via `audioEvent.audioPcm` — do not add a decode step there.

## Even Hub SDK (relay path)

20. **Call order is mandatory:** `waitForEvenAppBridge()` → `createStartUpPageContainer()` (one-shot, must return `success`) → everything else. Glasses-mic `audioControl` and `imuControl` fail without it.
21. **Exactly one container per page has `isEventCapture: 1`.** Zero or multiple is undefined behaviour.
22. **`zOrderIndex` is all-or-nothing per page and must be unique** — otherwise SDK validation rejects the page before it reaches native.
23. **Limits:** `containerTotalNum` 1–12, max 8 text containers, max 4 image containers, `textContainerUpgrade` ≤ 2000 chars, list ≤ 20 items × 64 chars.
24. **`updateImageRawData` calls must be serial** — await each before the next.
25. **Always call the returned unsubscribe** for `onEvenHubEvent` / `onDeviceStatusChanged` / `onLaunchSource`; `onLaunchSource` fires once, so register it immediately after bridge init.
26. **Prefer `textContainerUpgrade` over `rebuildPageContainer`** for content changes — in-place, no flicker. Rebuild only for actual layout changes.
27. **Always `?? 0` when reading a numeric event field.** The protobuf transport omits zero values, so `eventType` for a single press and `currentSelectItemIndex` for the first row both arrive as `undefined`. `x === CLICK_EVENT` (i.e. `=== 0`) never matches, and the bug is invisible in review — the code reads correctly and does nothing.
28. **Clicks arrive as `sysEvent`, not `textEvent`.** Only scroll gestures produce `textEvent`. Any handler that expects a click on a text container is dead code.
29. **Handle `SYSTEM_EXIT_EVENT` (7) and `ABNORMAL_EXIT_EVENT` (6)** — stop the mic and IMU there. Skipping them leaves the glasses mic streaming after the wearer exits.
30. **Never tear down before `shutDownPageContainer(1)`** — the wearer can still cancel the dialog, and an app that cancelled into a dead event loop is worse than one that exited.
