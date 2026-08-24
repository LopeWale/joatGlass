# apps/mac — the brain

Python 3.12+ package. Receives audio/IMU/input from the Hub plugin, runs STT and reasoning, renders replies to the HUD.

## Run the tests

From the repo root (not this directory — `pyproject.toml` sets `pythonpath`):

```bash
pip install -e ".[dev]"
pytest tests/ -q
```

38 tests today: protocol parsing/serialization plus integration tests that stand up a real WebSocket server and round-trip against it.

## What exists

| Module | Status |
|---|---|
| `jarvis/core/protocol.py` | Done — mirror of `types.ts`, with `parse_uplink` and `to_wire` |
| `jarvis/core/interfaces.py` | Done — `GlassesTransport`, `SttEngine`, `Confirmer` ABCs |
| `jarvis/ble/hub_gateway.py` | Done — `HubGatewayTransport`, the WebSocket server the plugin dials |
| `jarvis/audio/whisperkit.py` | Scaffold — shells out to `whisperkit-cli`, untested against the real binary |
| `jarvis/services/` | Empty — the Orchestrator goes here |
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
