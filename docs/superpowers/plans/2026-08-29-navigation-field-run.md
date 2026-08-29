# Navigation Field Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture one walking and one cycling experimental navigation field run without retaining routes, destinations, spoken text, coordinates, or motion values.

**Architecture:** The Hub emits only fixed navigation-cue names and already-bucketed location quality metadata through the existing opt-in `g2.diagnostic` channel. The Mac diagnostic store supplies receipt timestamps and the existing physical runner converts the redacted trace into an owner-only field-run summary plus start/end lens-video markers. Direct speech starts the existing `NavigationSession`; it does not rely on a sidebar being open.

**Tech Stack:** TypeScript/Vite Hub plugin, Python asyncio/WebSocket gateway, `pytest`, `vitest`, official Even Hub simulator, G2 lens video.

**Spec:** User request of 2026-08-29: one walking and one cycling experimental field run; bounded semantic state, location-quality buckets, HUD cue timestamps, synchronized lens-video markers; no raw routes or spoken destinations.

## Global Constraints

- Keep `g2.diagnostic` opt-in, owner-only, capped at 512 events, and content-free.
- Never persist latitude, longitude, route geometry, place labels, spoken/transcribed text, device identifiers, audio, or tokens.
- Accept direct speech only for an explicit `navigate to …`, `directions to …`, or `take me to …` command, optionally preceded by `JOAT`.
- Treat bridge acknowledgements and simulator frames as distinct from lens-visible proof.
- Run physical walking/cycling only after the new package is installed, the phone is foreground/unlocked, and the paired G2 is available.

---

### Task 1: Make direct voice navigation match the wearer command

**Files:**

- Modify: `apps/mac/joat/services/navigation.py:40-54`
- Test: `tests/unit/test_navigation.py:195-203`

**Interfaces:**

- Consumes: a transcript string from `SessionManager`.
- Produces: `navigation_query(text) -> str | None`; it returns only the destination to the in-memory `NavigationSession` and never records it.

- [x] **Step 1: Write the failing test**

```python
assert navigation_query("JOAT, navigate to the library") == "the library"
assert navigation_query("joat directions to 2301 N Lincoln Ave") == "2301 N Lincoln Ave"
```

- [x] **Step 2: Run the focused test to verify it fails**

Run: `PYTHONPATH=apps/mac .venv/bin/python -m pytest -q tests/unit/test_navigation.py -k explicit_navigation_invocation`

Expected: the wake-word forms return `None`.

- [x] **Step 3: Implement the minimum parser change**

```python
request = re.sub(r"^joat[,:]?\\s+", "", request, flags=re.IGNORECASE)
```

Keep the existing verb-and-`to` grammar; do not make arbitrary speech a destination.

- [x] **Step 4: Run the focused test to verify it passes**

Run: `PYTHONPATH=apps/mac .venv/bin/python -m pytest -q tests/unit/test_navigation.py -k explicit_navigation_invocation`

- [ ] **Step 5: Commit**

Do not commit in the shared checkout. Stage only these two paths when the owner serializes a commit.

### Task 2: Emit bounded location quality and semantic navigation cues

**Files:**

- Modify: `apps/hub-plugin/src/LocationSessionManager.ts:recordProbe`
- Modify: `apps/hub-plugin/src/NavigationController.ts:present,renderProgress`
- Modify: `apps/hub-plugin/src/G2DiagnosticTrace.ts`
- Modify: `apps/mac/joat/core/protocol.py:_parse_g2_diagnostic`
- Test: `apps/hub-plugin/src/LocationSessionManager.test.ts`
- Test: `apps/hub-plugin/src/NavigationController.test.ts`
- Test: `tests/unit/test_protocol.py`

**Interfaces:**

- Consumes: high-accuracy location callbacks and `NavigationHudModel` values.
- Produces: `location_probe` and `navigation_location` quality events with cadence in the fixed enum `first|lte1s|1_5s|5_30s|gt30s`, and `navigation_cue {cue, outcome}` for exact HUD states only. The navigation-location event comes from the real navigation high-accuracy profile, not the standalone probe.

- [x] **Step 1: Write failing tests**

```ts
expect(samples[1]).toMatchObject({ cadence: 'lte1s' });
expect(diagnostics.record).toHaveBeenCalledWith({
  type: 'navigation_cue', cue: 'mode', outcome: 'success',
});
```

```python
assert parse_uplink({"kind": "g2.diagnostic", "event": {
    "type": "navigation_cue", "cue": "guidance", "outcome": "success",
}}).event["cue"] == "guidance"
```

- [x] **Step 2: Run the focused tests to verify they fail**

Run: `npm test -- --run src/LocationSessionManager.test.ts src/NavigationController.test.ts` from `apps/hub-plugin`, then `PYTHONPATH=apps/mac .venv/bin/python -m pytest -q tests/unit/test_protocol.py`.

- [x] **Step 3: Implement the fixed vocabulary**

Replace numeric cadence on the Hub uplink with the enum. Normalize legacy `cadenceMs` only inside the Python parser before it reaches receipt storage. Record a cue only after `hud.showNavigation()` resolves, and use the existing store timestamp rather than sending a browser clock.

- [x] **Step 4: Run the focused tests to verify they pass**

Run the commands in Step 2. Confirm that any unsupported `text`, `latitude`, `longitude`, or route field raises `ProtocolError`.

- [ ] **Step 5: Commit**

Do not commit in the shared checkout. Stage only the listed paths when serialized.

### Task 3: Produce the owner-only field-run receipt and video markers

**Files:**

- Modify: `scripts/real_g2_location_probe_trace.py`
- Test: `tests/unit/test_real_g2_location_probe_trace.py`
- Create: `docs/REAL_G2_NAVIGATION_FIELD_RUN.md`

**Interfaces:**

- Consumes: the already-sanitized `joat.g2-diagnostic.v2` receipt.
- Produces: a `joat.g2-navigation-field-run.v1` summary from normal JOAT's redacted receipt, with fixed cue labels, store-assigned cue timestamps, aggregate navigation-session quality buckets, and start/end marker timestamps.

- [x] **Step 1: Write failing summary tests**

```python
assert summary["locationQuality"]["accuracy"] == {"lte10": 2}
assert summary["hudCues"] == [{"cue": "guidance", "outcome": "success", "recordedAtMs": 12}]
assert summary["lensVideoMarkers"] == [{"name": "start", "recordedAtMs": 10}]
assert "destination" not in json.dumps(summary).lower()
```

- [x] **Step 2: Run the focused test to verify it fails**

Run: `PYTHONPATH=apps/mac .venv/bin/python -m pytest -q tests/unit/test_real_g2_location_probe_trace.py`

- [x] **Step 3: Implement minimum redacted aggregation**

Use only receipt-owned `recordedAtMs`, the fixed cue enum, and count maps for the existing quality buckets. The first acknowledged destination-confirmation screen is the `start` marker and a successful stop/arrival state is the `end` marker. Keep all paths, files, console, and JSON free of a destination or route.

- [x] **Step 4: Write the operator checklist**

The checklist requires a single continuous lens video, a start-marker tap before motion, a direct voice command, on-glass confirmation and walking/cycling choice, a cue visible in the video, an end-marker tap, and a `pass|fail|inconclusive` result. It tells the operator to share the short video plus the two owner-only JSON files, not spoken text or route details.

- [x] **Step 5: Run the focused test to verify it passes**

Run the command in Step 2 and verify output files are mode `0600`.

### Task 4: Verify the causal navigation flow without substituting fixtures for G2 proof

**Files:**

- Modify: `scripts/simulator_navigation_action_flow_acceptance.py` only if the new cue receipt needs an assertion.
- Test: `tests/unit/test_simulator_navigation_action_flow_acceptance.py`

**Interfaces:**

- Consumes: production EventRouter, NavigationController, WebSocket gateway, and deterministic search provider.
- Produces: a simulator receipt that connects each input action to its semantic action and visual state.

- [x] **Step 1: Verify the existing expected visible-state and semantic-action assertions**

```python
assert receipt.semantic_action == "select_mode"
assert receipt.visible_view == "uncertain"
```

- [x] **Step 2: Run the focused test, then the official simulator runner**

Run: `PYTHONPATH=apps/mac .venv/bin/python -m pytest -q tests/unit/test_simulator_navigation_action_flow_acceptance.py`.

Run the documented simulator command on isolated ports. Inspect that click → mode, scroll → selection, click → locating/error, retry, and cancel are all causal transitions.

- [x] **Step 3: Record evidence limits**

The simulator proves action routing and framebuffer changes only. It does not prove phone GPS, cue timing over BLE, left-temple source routing, or lens visibility.

### Task 5: Build and execute only when the paired device is ready

**Files:**

- Modify: `apps/hub-plugin/app.json`, `package.json`, `package-lock.json`, and generated `joat.ehpk` only after all code gates pass.

- [ ] **Step 1: Build the exact package**

Run: `npm test && npm run pack` in `apps/hub-plugin`; record version, SHA-256, byte count, and no DEV fixture in package stage.

- [ ] **Step 2: Upload to the existing private JOAT project and install it**

Require an explicit portal receipt and user confirmation that the phone installed the named version. This does not establish lens behavior.

- [ ] **Step 3: Run walking and cycling separately**

Use the checklist. Run only with the paired G2 and foreground/unlocked phone. Stop immediately on a non-visible HUD, missing high profile, missing quality sample, absent cue, or marker timeout.

- [ ] **Step 4: Report evidence separately**

Report source/test proof, official simulator proof, portal/install proof, and physical lens-video proof as independent results. Never call a bridge acknowledgement a visible HUD.

## Self-Review

- Spec coverage: Task 1 covers direct voice; Task 2 fixed semantic and quality telemetry; Task 3 owner-only receipt/markers/checklist; Task 4 action-flow causality; Task 5 two physical modes and evidence separation.
- No placeholders: every task names files, types, tests, and commands.
- Type consistency: `navigation_cue` is the sole new semantic event; `cadence` is a fixed enum; the field-run summary reads only protocol-sanitized event fields.

## Execution Handoff

The user explicitly requested implementation in this task, so execute this plan inline with a focused test gate after every task. Do not create mock HUDs or treat simulator output as physical proof.
