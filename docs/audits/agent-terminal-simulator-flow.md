# Persistent Agent Terminal simulator acceptance

The acceptance runner uses the official Even simulator and a production plugin build. It never accesses the installed JOAT engine, pairing files, saved profiles, remote credentials, or the phone/glasses connection.

```sh
PYTHONPATH=apps/mac .venv/bin/python scripts/simulator_agent_terminal_flow.py
```

The isolated ports are gateway 18980, local TLS A2A services 18981, plugin 5298, and official simulator automation 9998. Occupied ports cause a failure before startup. Cleanup stops only processes created by the runner, closes local services and adapters, and removes the temporary TLS key.

## Interaction and conversation contract

The simulator opens the actual `context_menu`, selects Agent Terminal using `down`/`click`, and chooses OpenClaw or Hermes. Three legacy sessions remain present internally to prove the picker excludes them.

Each agent receives three consecutive inputs without leaving its conversation:

1. Introduce a distinct fact: OpenClaw knows “cobalt beacon”; Hermes knows “amber compass.”
2. Ask a follow-up that depends on the earlier fact. The acknowledged inline input must keep the prior reply visible.
3. Request a long, three-paragraph answer. The full assistant text must match the service response, and the actual simulator must scroll to separate BEGIN, MIDDLE, and END markers.

The runner then returns to the chooser and reopens the conversation. The conversation UUID, ordered user and assistant messages, message identities, and visible middle reading position must survive. Cancelling the next input must preserve that conversation. A fourth OpenClaw request is held in flight to verify that busy entry still opens the chooser and that a later response does not displace the wearer from older text they are reading.

## Independent checks and deliberate synthetic boundaries

The local TLS services are stateful protocol oracles. They require authenticated initialization, unique JSON-RPC and message IDs, the expected `contextId`, no resumption of a completed task, exact prior conversation turns, and the expected next prompt. A request with the other agent's fact, lost history, wrong context, duplicated identity, or an unexpected extra turn is rejected. Final counts must be two initialization probes and seven sends, distributed four to OpenClaw and three to Hermes.

Production `HubGatewayTransport`, `CapabilityRouter`, `TerminalOpeningController`, `AgentTerminalRoster`, `SessionManager`, `PromptedSpeechRuntime`, `A2ATerminalAgent`, and `A2AHttpTransport` handle the actual interaction and response path. The production plugin and official simulator handle presentation, scrolling, and gestures.

Only the external agent service and final speech-to-text result are replaced. A transcript is supplied only after the simulator acknowledges the actual current input prompt, carrying its exact prompt and connection identity. This does not claim to measure speech recognition or external provider uptime.

Rendered text is checked with Apple's Vision OCR over the simulator's actual framebuffer, independently of the renderer's layout calculations. Screenshots preserve raw RGBA pixels and an optical preview. Captures wait for stable content-area pixels, excluding the changing clock, before OCR and visual review.

## Evidence and result interpretation

Artifacts are written to `dist/mac-workspace/control-audit/agent-terminal-persistent/`: numbered or named screenshots, recognized text, semantic downlink frames, per-request protocol receipts, provider violations, and final conversation identities. `receipt.json` is removed before every run and is written with `result: passed` only after all assertions succeed. `observed.json` survives failures for diagnosis.

A prior, simpler acceptance run established single-turn navigation and Back for both agents. That older result is not proof of persistent multi-turn conversations. The strengthened runner above must pass on the current implementation before this slice is accepted.

## Final implementation result

The final-source execution completed successfully with **47 captured screens, nine JSON-RPC requests, and zero provider violations**. Both agents retained three consecutive turns. The fourth OpenClaw turn verified a response arriving while older text was being read. The runner's independent checks confirmed 14 stable message identities and exact transcript-region pixel equality across both agents' Back/reopen operations and the held OpenClaw update.

The inline input, follow-up answers, long paragraphs, restored reading positions, cancelled input, busy chooser, and new-output indicator were visually inspected. The new-output indicator increased from six to eight newer rows without moving the older text; actual down gestures then revealed the new answer.

The final replay includes SHA-256 fingerprints for 205 plugin and Mac source inputs plus the generated JavaScript bundle. The source fingerprint was captured before simulator startup and compared for exact equality after the final screenshot. No production input changed during acceptance. The held response screenshot also explicitly verifies the terminal header says WORKING, matching its chooser state.

Cleanup verified ports 18980, 18981, 5298, and 9998 closed and the temporary TLS key removed. No physical or installed-engine connection was used.

One OCR-only retry was needed: Vision confused the arbitrary fixture word “lantern” with “lantem.” The fixture fact was changed to “cobalt beacon,” preserving exact history, agent-separation, and follow-up assertions. No product behavior or acceptance condition was weakened.
