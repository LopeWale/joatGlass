# Agent Terminal simulator flow

This acceptance uses the official Even simulator and the current production plugin build. It never connects to the installed JOAT engine or reads pairing files, saved profiles, or remote credentials. The phone/glasses connection is outside this runner.

Run from the repository root:

```sh
PYTHONPATH=apps/mac .venv/bin/python scripts/simulator_agent_terminal_flow.py
```

The runner refuses to start if any of its isolated ports are occupied: gateway 18980, local TLS A2A service 18981, built plugin 5298, and official simulator automation 9998. It stops only subprocesses it created. The temporary TLS private key is removed at cleanup.

## What is exercised

- The official simulator's `context_menu`, `down`, `click`, and `double_click` inputs open Agent Terminal through the real feature menu.
- Production `HubGatewayTransport`, `CapabilityRouter`, `TerminalOpeningController`, and `AgentTerminalRoster` prepare the selected agent and paint its conversation.
- Three internal legacy sessions remain present in the roster but are excluded from the user-facing chooser. The observed picker must contain exactly OpenClaw and Hermes.
- A simulator tap opens the production spoken-input question. The production prompt dispatcher must receive the simulator's acknowledgment before accepting a final transcript.
- Production `A2ATerminalAgent` and `A2AHttpTransport` authenticate against local TLS JSON-RPC services, perform initialization, submit the request, parse its completed task, and publish the reply.
- Each agent's Back gesture returns to the two-agent chooser. Additional checks cover cancelling an input question and opening the chooser while a request is held in flight.

The deliberate synthetic boundaries are **the final speech-to-text result and the remote agent service**. The injected transcript is admitted only after the actual simulator question acknowledgment, using its current prompt and connection identity. The local responses explicitly say “simulator reply.” This verifies interaction, routing, authentication protocol, and presentation; it does not claim to measure speech recognition or the external providers' availability.

## Evidence

The runner retains numbered optical screenshots, their original RGBA framebuffers, JSON-RPC method receipts, and the production downlink frames in `dist/mac-workspace/control-audit/agent-terminal-isolated/`. It requires two identical content-area framebuffer samples after settling; the changing top clock is excluded from comparison. Semantic assertions independently verify both current replies and the chooser destinations. `receipt.json` is written only after all assertions pass; stale success receipts are deleted at startup. `observed.json` is retained even after a failure for diagnosis.

The numbered sequence is Home, feature menu, selected Agent Terminal menu entry, two-agent chooser, OpenClaw conversation/question/reply/Back, Hermes conversation/question/reply/Back, cancelled question, and chooser while busy.

## Visual findings from the implementation run

The inspected chooser shows only OpenClaw and Hermes with “Tap to connect,” becoming “Ready” after a completed request. Each conversation shows the selected agent, its transcript, and “tap: speak · swipe: scroll · double-click: back.” Back from a completed conversation returns to the chooser. Cancelling a question restores the original OpenClaw reply. Opening the feature menu while the OpenClaw HTTP response is held still returns to the chooser, showing OpenClaw “Working” and Hermes “Ready.”

An early screenshot taken 0.6 seconds after a semantic update showed a ready header and a previous running footer. The settled frame showed the correct footer. The runner was strengthened to wait for stable framebuffer samples instead of treating the first screenshot after a downlink as a completed paint. An extra test also initially assumed the chooser retained its second-row selection; the native list resets to the first row. The final cancellation check explicitly selects and asserts OpenClaw, instead of accepting whichever agent happened to open.

## Committed-source replay

The final replay used a clean exported source snapshot through `859870b`, including the shared Back routing in `1621af9`. Its evidence is in `dist/mac-workspace/control-audit/agent-terminal-committed/`. The final question footer says “speak your answer · double-click: cancel”; an earlier committed-source capture exposed the old left-only wording and was superseded. The scoped snapshot passes TypeScript compilation, 255 UI tests and 248 roster/opening tests. The simulator walkthrough and visual inspection remain separate acceptance evidence from those tests.
