# apps/mac — the brain

Python 3.12+ package. Receives audio/IMU/input from the Hub plugin, runs STT and reasoning, renders replies to the HUD.

## Run the tests

From the repo root (not this directory — `pyproject.toml` sets `pythonpath`):

```bash
pip install -e ".[dev]"
pytest tests/ -q
```

51 tests today: protocol parsing/serialization, VAD segmentation, orchestrator flow, plus integration tests that stand up a real WebSocket server and round-trip against it.

## What exists

| Module | Status |
|---|---|
| `joat/core/protocol.py` | Done — mirror of `types.ts`, with `parse_uplink` and `to_wire` |
| `joat/core/interfaces.py` | Done — `GlassesTransport`, `SttEngine`, `Confirmer` ABCs |
| `joat/ble/hub_gateway.py` | Done — `HubGatewayTransport`, the WebSocket server the plugin dials |
| `joat/audio/whisperkit.py` | Done — shells out to `whisperkit-cli`, verified against the real binary |
| `joat/audio/vad.py` | Done — RMS utterance segmentation (thresholds uncalibrated) |
| `joat/audio/whisper_serve.py` | Done — resident STT via `whisperkit-cli serve` (~0.85 s/utterance; default `--stt-mode serve`) |
| `joat/services/orchestrator.py` | Done — Phase-0 voice loop: mic → VAD → STT → HUD transcript |
| `joat/agents/`, `verifier/`, `channels/`, `computeruse/` | Empty — Phases 2–4 |

## Protocol drift is the main hazard

`joat/core/protocol.py` and `apps/hub-plugin/src/types.ts` describe the same wire format in two languages. **Change them in the same commit.** `tests/unit/test_protocol.py` uses the exact JSON the plugin emits, so drift fails there rather than in the field — but only if the TS side is the thing you updated first.

Naming convention across the boundary: the wire is **camelCase** (`actionId`, `containerId`, `paceMs`), Python is **snake_case**. Every `to_wire()` and every `parse_uplink` branch does that translation explicitly. Don't add a generic auto-converter — the explicit mapping is what makes a missing field a loud parse error instead of a silent `None`.

## Design notes worth knowing before you extend it

**Audio sequence gaps are meaningful.** `MacLink` on the plugin side sheds the *oldest* queued frames when the socket is down, so a `seq` jump means real audio is missing. `HubGatewayTransport` logs it; the STT layer should call `reset()` rather than splice across the hole and hallucinate a sentence that was never spoken.

**Confirms fail closed.** `request_confirm()` returns `False` on timeout. Silence is not consent — the wearer may not even be looking at the HUD. Tested in `test_timeout_fails_closed` and `test_answer_for_a_different_action_does_not_resolve`.

**One plugin connection at a time.** The newest wins and the previous is closed. Reconnect-after-sleep is common; two genuinely concurrent plugins are not a real scenario.

**Unparseable messages are dropped, not fatal.** A newer plugin talking to an older brain is normal deploy skew. Logged and ignored.

## Next

The `Orchestrator` in `joat/services/`: subscribe to transport audio, feed `SttEngine`, route transcripts to a reply, and send `HudText` back. That closes Phase 0.

## Deploying the core engine to a new Mac (mini, Studio, anything)

One command, idempotent — no manual reconfiguration of the Python voice/G2
engine:

```bash
git clone <this repo> && cd joatGlass
./scripts/setup_brain.sh --service --warm
```

The script installs Python ≥3.12 and `whisperkit-cli` (via Homebrew) if missing,
creates the venv, installs the package, runs the test suite, pre-downloads the
Whisper model (`--warm`), and installs a launchd agent (`--service`) so the
core brain starts at login and restarts if it dies. Logs:
`~/Library/Logs/joat/brain.log`.

This raw service is deliberately **not** a complete Agent Terminal deployment.
It has no `JOAT.app` native bridge, so it cannot use the Keychain-backed
OpenClaw device identity/token; the engine reports Agent Terminal as
`native_credentials_unavailable` instead of silently attempting Hermes with a
weaker credential. For Brainstorm/Agent Terminal on a home Mac mini, run
`./scripts/setup_brain.sh --warm` **without** `--service`, then install and
launch `/Applications/JOAT.app` under that mini's logged-in user. Do not leave
the raw `com.joat.brain` service running alongside JOAT.app: both own the
same engine port, and the app is the supported owner for Agent Terminal. The
app discovers the conventional `~/joatGlass` clone, starts the same Python
child, owns the native bridge, and can be enabled once as a Login Item. Daily
glasses use does not require opening the Mac app.

Each home brain has one-time setup: its `openclaw.json` contains only the
approved Gateway origin and `hermes` agent ID; JOAT.app keeps the resulting
device identity/token in that Mac's Keychain and the OpenClaw host approves
that device. See [the Mac app deployment contract](../../docs/MAC_APP.md#home-mac-mini-brain-contract)
before treating a new mini as an Agent Terminal failover.

The one step it will not automate: adding the new Mac's hostname to the plugin's
`app.json` network whitelist (and `VITE_MAC_WS_URL`). That is a deliberate
security decision — the script detects the mismatch and tells you exactly what
to add, but a human commits it. The whitelist already carries the MacBook and
the mac mini; a third machine is one more pair of `ws://`/`wss://` entries.

Model choice: `JOAT_STT_MODEL=<name>` overrides the default `large-v3_turbo`
(e.g. `base` on a low-RAM machine).
