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
| `jarvis/core/protocol.py` | Done — mirror of `types.ts`, with `parse_uplink` and `to_wire` |
| `jarvis/core/interfaces.py` | Done — `GlassesTransport`, `SttEngine`, `Confirmer` ABCs |
| `jarvis/ble/hub_gateway.py` | Done — `HubGatewayTransport`, the WebSocket server the plugin dials |
| `jarvis/audio/whisperkit.py` | Done — shells out to `whisperkit-cli`, verified against the real binary |
| `jarvis/audio/vad.py` | Done — RMS utterance segmentation (thresholds uncalibrated) |
| `jarvis/services/orchestrator.py` | Done — Phase-0 voice loop: mic → VAD → STT → HUD transcript |
| `jarvis/agents/`, `verifier/`, `channels/`, `computeruse/` | Empty — Phases 2–4 |

## Protocol drift is the main hazard

`jarvis/core/protocol.py` and `apps/hub-plugin/src/types.ts` describe the same wire format in two languages. **Change them in the same commit.** `tests/unit/test_protocol.py` uses the exact JSON the plugin emits, so drift fails there rather than in the field — but only if the TS side is the thing you updated first.

Naming convention across the boundary: the wire is **camelCase** (`actionId`, `containerId`, `paceMs`), Python is **snake_case**. Every `to_wire()` and every `parse_uplink` branch does that translation explicitly. Don't add a generic auto-converter — the explicit mapping is what makes a missing field a loud parse error instead of a silent `None`.

## Design notes worth knowing before you extend it

**Audio sequence gaps are meaningful.** `MacLink` on the plugin side sheds the *oldest* queued frames when the socket is down, so a `seq` jump means real audio is missing. `HubGatewayTransport` logs it; the STT layer should call `reset()` rather than splice across the hole and hallucinate a sentence that was never spoken.

**Confirms fail closed.** `request_confirm()` returns `False` on timeout. Silence is not consent — the wearer may not even be looking at the HUD. Tested in `test_timeout_fails_closed` and `test_answer_for_a_different_action_does_not_resolve`.

**One plugin connection at a time.** The newest wins and the previous is closed. Reconnect-after-sleep is common; two genuinely concurrent plugins are not a real scenario.

**Unparseable messages are dropped, not fatal.** A newer plugin talking to an older brain is normal deploy skew. Logged and ignored.

## Next

The `Orchestrator` in `jarvis/services/`: subscribe to transport audio, feed `SttEngine`, route transcripts to a reply, and send `HudText` back. That closes Phase 0.

## Deploying to a new Mac (mini, Studio, anything)

One command, idempotent — no manual reconfiguration:

```bash
git clone <this repo> && cd joatGlass
./scripts/setup_brain.sh --service --warm
```

The script installs Python ≥3.12 and `whisperkit-cli` (via Homebrew) if missing,
creates the venv, installs the package, runs the test suite, pre-downloads the
Whisper model (`--warm`), and installs a launchd agent (`--service`) so the
brain starts at login and restarts if it dies. Logs: `~/Library/Logs/joat/brain.log`.

The one step it will not automate: adding the new Mac's hostname to the plugin's
`app.json` network whitelist (and `VITE_MAC_WS_URL`). That is a deliberate
security decision — the script detects the mismatch and tells you exactly what
to add, but a human commits it. The whitelist already carries the MacBook and
the mac mini; a third machine is one more pair of `ws://`/`wss://` entries.

Model choice: `JOAT_STT_MODEL=<name>` overrides the default `large-v3_turbo`
(e.g. `base` on a low-RAM machine).
