# Resident WhisperKit cold-start recovery

## Change contract and plan

Keep the resident STT adapter in the voice session after a slow or transient
first load, without extending the 30-second foreground startup wait. Preserve
shutdown, local-only audio, model-directory selection, prompt-echo filtering,
warm-request ownership and the existing paired G2 transport. No packaging,
upload, app installation or production restart is part of this source change.

Execution order: inspect current source and production startup logs; reproduce
the timeout with an isolated process; add failing lifecycle regressions; keep
one recoverable adapter; run subprocess, privacy and pairing regressions; report
physical-G2 latency as a separate acceptance gate.

## Evidence collected on 2026-09-03

The production `~/Library/Logs/joat/brain.log` contains three evening failures:
21:05:25, 21:22:21 and 21:23:29. Each reports connection refused after 30 seconds
and immediately selects batch STT. Earlier failures also appear on September 2;
the successful 19:46 startup that day took approximately five seconds.

The old source killed its child at that deadline, returned `False`, and
`__main__.py` discarded the resident adapter for the rest of the session. The
logs alone did not distinguish an exited child from a slow live child.

Two isolated launches of installed `whisperkit-cli v1.1.0`, using the existing
`large-v3_turbo` weights and unused loopback ports, established the slow-live
case:

- The baseline probe found the child alive but refusing connections at 30.06 s;
  it then terminated and reaped only that probe child.
- An extended probe found the child alive at 30 and 60 s. Its first successful
  100 ms silence transcription canary arrived at 80.17 s. It was then reaped.
- A fresh post-change real-CLI smoke was warm: ready at 5.05 s; close took
  0.01 s and left neither an owned child nor an unjoined canary.

These are isolated Mac process measurements, not physical-G2 latency. No cache
was deleted, evicted or moved, so they do not assert a reboot-cold ANE state or
identify which vendor loading phase caused the delay. No wearer audio was used
or retained. The production service and port 50060 were not replaced.

## Recovery decision

Increasing a fixed timeout would still turn a longer load into session-long
batch STT. Restarting at 30 seconds discards useful loading progress. Instead:

- `start()` waits up to the existing 30-second foreground budget, without
  cancelling its canary when that budget expires. `True` means the adapter owns
  STT, possibly through temporary batch; it does not claim resident readiness.
- The same live child keeps loading. One sequential transcription-canary loop
  establishes readiness; TCP acceptance or malformed JSON is insufficient.
- A child that exits, or a failed spawn, gets a new attempt after a 30-second
  cooldown. There is no unbounded spawn loop or retry on every utterance.
- A complete utterance chooses one backend. If recovery finishes during batch
  transcription, that batch request completes once; the next utterance uses
  resident STT. Normal warm requests retain their existing non-dropping wait.
- Shutdown cancels and joins recovery, terminates/reaps owned resident and batch
  children, and joins the actual canary worker before a model switch/restart.
  Failed model switches restore the previous model but report failure to the
  model registry, rather than falsely claiming the requested model was selected.
- Temporary batch receives the resident model and exact selected download
  directory. Existing weights win; fresh weights remain under Application
  Support. Explicit standalone batch defaults are unchanged.
- Logs use fixed states and error classes/exit codes, not server response bodies,
  CLI stderr, audio, transcripts or exact per-utterance timings. The existing
  opt-in, coarse voice-latency receipt remains unchanged.

The production changes are confined to the two STT adapters. Caller composition,
Hub/phone pairing, authentication, SDK audio delivery and HUD rendering are not
changed by this patch.

## Repeatable source verification

The subprocess fixture uses an executable synthetic CLI, real loopback HTTP,
gate-controlled readiness and synthetic PCM. It checks slow binding, TCP-open
but model-not-ready responses, first-child exit/retry, exact batch model path,
shutdown after the foreground timeout, in-flight batch cleanup and forced kill.
Unit tests cover late/hung canary ownership, backend selection across an
in-flight batch, retry cadence, cancellation, model rollback and privacy.

Fast regression command:

```sh
.venv/bin/python -m pytest tests/unit/test_whisper_serve.py tests/integration/test_whisper_cold_start.py -q
```

Production-budget controlled reproduction (holds the child cold past the real
30-second gate, then releases it after one batch utterance):

```sh
JOAT_COLD_START_REAL_BUDGET=1 .venv/bin/python -m pytest tests/integration/test_whisper_cold_start.py -q -k 'cold_child and False' --durations=1
```

The final production-budget run passed in 30.09 s, using the same child before and
after recovery. This proves the adapter's recovery transition, not a real
Whisper inference time.

Final verification:

- 229 source tests passed, including 43 STT unit/subprocess regressions and the
  engine-shutdown, main, models, pairing, real-loopback Hub gateway,
  voice-latency privacy and orchestrator suites.
- Strict `.venv/bin/python -m mypy`: no issues in 98 source files.
- The concurrent close/flush-cancel case exercised an actual batch child that
  ignored TERM; cleanup escalated to KILL and reaped it (5.15 s).
- Scoped `git diff --check` passed. No probe WhisperKit/stub processes remained.

The broader source command was:

```sh
.venv/bin/python -m pytest tests/unit/test_whisper_serve.py tests/integration/test_whisper_cold_start.py tests/unit/test_engine_shutdown.py tests/unit/test_main.py tests/unit/test_models.py tests/unit/test_pairing.py tests/integration/test_hub_gateway.py tests/unit/test_voice_latency.py tests/unit/test_orchestrator.py -q
```

## Physical-G2 acceptance — not yet measured

No fresh wearer-speech-end → first lens-visible reply measurement was captured
in this task. Source tests, localhost canaries, historical logs and gateway
acknowledgements cannot close that gate.

For the follow-up, use a verified source-backed Mac runtime with this patch and
the existing paired phone/G2 installation; no Hub package/upload is required.
Coordinate the runtime restart with other work in this shared checkout first.
With wearer participation, capture synchronized speech-end and actual first
lens-visible reply for an utterance while warming and several after the
`whisper serve ready` transition. Keep the existing one-second endpointing and
first-reply rendering in that end-to-end measurement. If desired, explicitly
enable `--voice-latency-diagnostics` for its content-free coarse stages; do not
enable raw audio/transcript logging or treat those coarse stages as lens onset.

Report sample count, cold/warm state and physical latency separately from the
Mac/source figures above. The under-three-second physical voice-to-lens target
remains open until that run is observed.
