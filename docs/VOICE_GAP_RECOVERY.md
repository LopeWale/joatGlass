# Voice sequence loss and recovery

## Bounded repair plan

Trace SDK PCM → `AudioStreamer` batches → `MacLink` → gateway →
`AudioRouter` → `VoiceOrchestrator`. Separate missing numbered batches from
microphone loss before numbering, reconnect counter restarts, and event-loop
delays. Use only content-free summaries of existing logs.

The repair boundary is audio continuity: discard partial speech at a real gap
or a new connection, preserve already completed STT work, and erase all buffered
PCM (including an odd byte) when the microphone stops. Keep wake-word and
speaker gates, bounded pending-turn policy, pairing, and diagnostics privacy.

Verify exact PCM loss/recovery with focused regressions, then exercise synthetic
PCM through the current Hub source, real gateway/orchestrator, and official Even
Realities Simulator. Inspect the rendered framebuffer. Retain content-free
receipts and source hashes; physical-G2 capture, packaging, upload, and live
deployment are separate gates and are not part of this repair.

## Findings from the existing log snapshot

Inspected `~/Library/Logs/joat/brain.log` through line 94,768 (10,413,971
bytes, ending September 3, 2026). Counts below are historical observations,
not a measurement of the current checkout or of a verified physical G2.
Only fixed event labels and numeric metadata were extracted; no transcripts,
PCM, pairing credentials, or device identifiers were copied into evidence.

| Window | Orchestrator resets | Unaddressed ignored | Completed turns dropped while busy |
| --- | ---: | ---: | ---: |
| August 25–30 | 381 | 492 | 148 |
| August 31 | 0 | 62 | 0 |
| September 1 | 14 | 70 | 0 |
| September 2 | 3 | 43 | 0 |
| September 3 | 0 | 0 | 0 |

**User correction:** September 3 had no usable glasses connection. The user
could not use the glasses because the plugin did not connect. These zeroes
therefore represent no usable voice traffic, not successful recovery or an
improved failure rate. The immediate live blocker is plugin startup/connection;
the earlier source and synthetic-simulator checks do not resolve that blocker.

All **398** resets followed a new `plugin connected` event within five seconds;
395 followed within one second. The gateway's same-connection `audio gap: seq`
warning had **zero matches**. The resets included 226 backward jumps, 170
forward jumps and two duplicates. All 17 September resets restarted at zero.
Within two seconds of those connections, 309 boundaries included
`replacing existing plugin connection`, mostly on August 26 and August 30.
This identifies socket churn and comparison across connection boundaries,
not evidence that all these batches were dropped from one continuous stream.
It does not identify the replacing client or establish why it reconnected.

There were 84 event-loop stall warnings (0.75–9.54 seconds), but none within
five seconds of these resets. The warning threshold and incomplete historical
instrumentation mean this cannot rule out every stall or stall-induced
disconnect. There is no basis here for adding a larger queue, tolerating
missing frames, or changing VAD/wake-word thresholds.

Unaddressed speech, other-speaker rejection, prompt echoes, empty STT and
busy-turn drops are different outcomes. The 148 old busy drops ended on August
30; current source already has one bounded latest-completed pending slot.
September 1–2 instead contain 12 pending retains and three replacements,
113 unaddressed results, 146 other-speaker exclusions and 24 empty-STT outcomes
(11 echo discards are a subset). Content-free counts cannot establish that
these were legitimate wearer requests or that a gap removed their wake words.

Example September 2 reset locations: log lines 92,979, 93,341 and 93,372.
Each follows a reconnect with sequence zero; the last two connect events are
at 93,336 and 93,367. No raw log excerpts are retained.

## Sequence contract and repair

1. The installed SDK supplies signed-16-bit LE, 16 kHz mono PCM, source and
   optional speaker metadata. It supplies no native sequence/timestamp to this
   path. Loss before `AudioStreamer.ingest()` is therefore invisible to the
   JOAT batch sequence.
2. `EventRouter` ingests audio synchronously before gesture gates.
   `AudioStreamer` numbers emitted batches, normally at most 3,200 samples.
   A delayed timer emits several contiguous messages, preserving PCM order;
   it does not itself create missing sequence numbers.
3. `MacLink` sends authenticated audio directly. Its bounded offline queue can
   evict messages; this remains real loss and must cause a discontinuity. No
   connected-path rate limiter or `bufferedAmount` shedding was found. Merely
   delaying the Python receive loop does not drop WebSocket messages.
4. Gateway sequence comparison is scoped to the current authenticated socket.
   `AudioFrame.connection_generation` carries that boundary; stale-socket
   dispatch is already rejected. `AudioRouter` preserves the frame, except
   that exclusive capture deliberately withholds it from conversation.
5. **Fixed:** the orchestrator now consumes that generation. A replacement
   clears VAD/pre-roll/source/speaker state even when its counter happens to
   equal the old counter plus one. A restart is reported as a stream boundary,
   not a missing batch. Every within-connection discontinuity still clears
   partial speech without interpolation, gap tolerance or wake-word bypass.
6. **Fixed:** an ingress reset cannot reset STT while the single-flight worker
   owns a completed utterance. Completed pending speech is also retained.
   This closes the asynchronous `SttEngine` interface race. The installed
   Whisper engines already detach their request PCM before awaiting inference;
   the held-feed regression is **not evidence they lost in-flight requests**.
7. **Fixed:** microphone stop erases the odd-byte carry as well as complete
   buffered samples and speaker state. It reserves one sequence position so
   mute/doff/resume cannot splice speech on the same socket. Repeated stop is
   idempotent. The skipped number denotes a pause, not a measured lost packet
   or an inferred duration. No wire fields or authentication policy changed.

The initial voice repair changes are limited to `AudioStreamer.ts`, `orchestrator.py`, and
an inaccurate causal comment in `hub_gateway.py`. Native mic lifecycle,
pairing, wake-word admission, speaker admission, pending-turn capacity and
diagnostic privacy remain governed by the existing production paths.

## Verification and retained evidence

- Before repair, focused regressions failed on cross-connection splicing,
  restart misclassification, erasure of a held completed STT turn, and missing
  microphone-stop discontinuity. After repair, **135 Python tests** passed
  across orchestrator and real-WebSocket gateway suites, including actual
  replacement sockets, explicit dropped batches, admission and pairing/log
  redaction. Strict mypy passed for both touched Python source files.
- **104 Hub tests** passed across `AudioStreamer`, `MacLink` and `EventRouter`;
  Hub typecheck passed. Delayed batching retains exact PCM; stop/resume clears
  the half-sample and emits a discontinuity.
- The official **Even Realities Simulator** ran current Hub source from a
  disposable snapshot. Only native `audioControl` was replaced in that
  snapshot to suppress the app's native microphone enable. Twelve synthetic batches went
  through production `AudioStreamer` and `MacLink`; batch six was omitted
  before real gateway audio dispatch. The real router/orchestrator processed
  exact pre-gap and post-gap PCM using a held local STT fixture and local
  command responses. No speech recognition or external model was used.
- Three distinct settled 576×288 framebuffers were visually inspected:
  ready/listening; **First turn preserved** with the first fixture query; then
  **Voice recovered** with the recovery fixture query and listening status.
  The first reply remained visible while the second fixture STT was held.
  No clipping or incomplete tiles were visible in these inspected states.
  The native optical previews were transient; their digests and inspection
  record are retained in [the evidence receipt](voice-gap-recovery-evidence.json).
- Read-only delegated reviews independently traced upstream batching and
  historical churn, then reviewed the repair for splicing/privacy defects.
  The main task independently recomputed the 398/398 connection correlation
  and zero gateway-gap count and ran the final integration gates.

Reproduce the synthetic visual check with an unused output directory:

```sh
PYTHONPATH=apps/mac:. .venv/bin/python scripts/simulator_voice_gap_acceptance.py \
  --out /tmp/joat-voice-gap-acceptance
```

The driver refuses occupied ports and writes a receipt requiring separate
visual inspection. Its source snapshot, synthetic PCM and subprocesses are
disposable. The retained receipt records the bytes used for the observed run;
it is not a claim that future edits or an installed package match that run.

## Remaining physical acceptance

No package, upload, live app restart or installation was performed. This
repair is **source and simulator verified**, not physical-G2 verified and not
a claim that real reconnect churn has stopped. Historical running commits and
physical-versus-simulator provenance cannot be recovered from these counters.

Physical acceptance must use one identified intended Hub client and the
repaired source on both ends: speak an explicitly addressed request, exercise
mute/resume and a controlled reconnect, then repeat the request. Verify actual
recognition and a visible lens response, confirm no pre-boundary phrase joins
the recovered turn, and compare stream-boundary versus same-stream gap counts.
If churn persists, capture its close reason and connection ownership with
content-free instrumentation before changing transport/backpressure policy.

## Follow-up: unavailable plugin and startup recovery

The September 3 zero-traffic correction changed the immediate acceptance
question to whether the plugin can start, connect, and display an answer.
Read-only local inspection found no gateway listener on port 8787, Tailscale
reported `Stopped`, and the local status file was stale. These facts establish
that this Mac was not a working live route at inspection time. They do not
establish the state of the phone or the other configured Mac. No service,
network, pairing, installed package or running app was changed.

Source inspection found a separately reproducible startup failure:
`main.ts` waits for HUD first light before `MacLink.connect()`, but the native
spine-label text write had no deadline. An unresolved host acknowledgement
therefore prevented any connection attempt. `HudVisualSurface` now applies
the existing text/tile write deadline to that call. The default remains
15 seconds per attempt, with the presenter's existing single retry before
text fallback. This is bounded recovery, not an instant-start claim. Other
startup bridge calls are outside this repair and may still fail or hang.

The first official-simulator fault run deliberately withheld the native spine
acknowledgement. It reached the text fallback and connected, but visual
inspection found only the fixture queries: assistant replies were absent.
The script's routing pass was **not** a visual acceptance pass. The fallback's
`HudPresenter.showAssistantOverlay()` rejected every reply without a visual
renderer. That guard now admits native text replies with the same retained
page, streaming throttle, dismissal and confirmation rules. Replies over
native lists use the existing dashboard reading area and restore the list;
they are not squeezed into its 40-pixel title.

A delegated review reproduced a delayed native reply overwriting a newly
rendered confirmation. Confirmation now waits for the active reply write
before rebuilding its exclusive page; its identity is rechecked afterward.
This wait deliberately does not time out and arm consent while an old write
can still land. Pairing, microphone admission, wake words, speaker gates and
the eight-second approval policy are unchanged.

Focused regressions cover nonsettling first light and late acknowledgement,
streamed fallback replies and restoration, native list reading-area recovery,
and both deferred and already-dispatched replies around exact confirmation
identities, including rejection of the prior native write. **110 Hub tests** passed across presenter, visual surface,
bootstrap and minimal presenter; Hub typecheck passed. The initial 135 Python
and 104 audio/transport Hub tests remain the separate voice-path evidence.

The final official-simulator run uses `--stall-spine` together with the same
one-batch loss and held-STT fixture. Its three settled optical frames were
inspected: listening, **First turn preserved**, then **Voice recovered**.
The receipt in [plugin-startup-recovery-evidence.json](plugin-startup-recovery-evidence.json)
records the failed first visual run, successful final run, source hashes and
local connectivity limits. An intervening run had a native menu obscuring
the replies and was not accepted as visual proof; its receipt is retained.
A separate final-source run without the startup fault visibly retained the
normal four-tile dashboard, clock, tabs and both reply overlays. The renderer,
page-builder and main design sources have no working-tree changes from this
task. The final fault run proves injected-fault recovery in
the simulator. It does not identify the user's physical startup failure or
prove real glasses have reconnected. No JOAT package or upload was produced.

Proofline's local `manage-evidence-decisions` workflow was applied to keep
source mechanism, engineering choice, historical observations and current
acceptance separate. Its bundled verifier passed the supplied synthetic
`valid-minimal.json` fixture. This proves the verifier runs locally; these
project receipts use JOAT's own schema, so no Proofline verification of them,
signed attestation or physical-device certification is claimed.
