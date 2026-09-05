# Investigation packet

Frame: JOAT-AUDIT-2026-09-04. Frozen September 4, 2026. Current dirty primary checkout, main 8386bf5. Original symptom: unreliable named features, incomplete messages/replies, slow assistant, failed navigation and chaotic UI; user wants conversation with feature use. Detailed quotations remain in the native task, not this packet.

Expected: complete wearer journeys for Assistant, Brainstorm, Agent Terminal, Messages, QuickNotes, Overview/News/Timer, application navigation and geographic Navigation. Observed baseline: user report plus current source mechanisms; no deployed-package-correlated failure rate or physical latency baseline. Falsifier: fresh version-correlated successful complete journeys, with physical speech-end-to-visible trace for the latency claim.

## Accepted link

| Why | Mechanism | Local level | Sources | Alternative | Verdict |
|---|---|---|---|---|---|
| 1 | Omitted feature actions are unavailable through the configured conversational tool interface; unmatched requests become displayed prose | L2 | Independent OpenAI, Anthropic, Google function/tool calling contracts | Conversational models do exist; MCP exposes other operations; permissions cannot create omitted schema | PASS, narrowly scoped |

Local anchors: __main__.py:463-479,492-510; agent_endpoint.py:399-415; tools.py:185-187; commands.py:71-141; capabilities.py:1829-1885; message_reply.py:42-44,309-314; orchestrator.py:449-475.

Sources, all accessed September 4, 2026:

- OpenAI, Function calling, living documentation: https://developers.openai.com/api/docs/guides/function-calling . Supports tools supplied, call received, application execution, result return and completion/further calls.
- Anthropic, Tool use with Claude, living documentation: https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview . Supports custom client execution, tool_use/tool_result loop and choice based on capability descriptions.
- Google, Function calling, updated September 3, 2026: https://ai.google.dev/gemini-api/docs/function-calling . Supports declarations, application execution, returned result, sequential/composite calls.

Independence: three separate implementers documenting their own interfaces, not one syndicated report. These sources establish an integration mechanism, not JOAT occurrence. No relevant prevalence statistic was used.

Challenge: high-confidence L2 PASS; do not say all tools are read-only (search can activate a panel), all CLI paths are incapable in principle, or every user symptom shares this cause. Bedrock for this bounded branch is the missing shared action interface. No deeper Why admitted; deeper claims about development process/history lack evidence.

## Distinct branches and limits

- Messages live WAL: production immutable connection reproduced missing a committed synthetic row. SQLite URI/WAL/pager sources support the mechanism but are one origin, so this is separate fixture evidence, not a further three-source Why link. No wearer DB read.
- QuickNotes control restoration: independent challenge confirms synthetic positional misdispatch after Back through the production Mac router and plugin menu, with fake painting/capture. Fake capture receives confirm_discard from a restored Save and finish row; no real recording was deleted. Back restores index2, so reaching Save and finish requires up twice; transient overlay must first be dismissed.
- Messages controller replay: actual serialized Mac frames cannot trigger history paging; Back clears the Mac row map and restored-row selection emits no response. Transport decoding and EventRouter are bypassed. Reader pagination/wrapping probe is a component demonstration with a manually assembled payload, not observed optical truncation. Independent review accepted these exact limits.
- Latency: serial turns, buffered Codex, shared lock/context and unbounded batch STT are confirmed source mechanisms. No physical timing magnitude assigned; historical STT startup data is explicitly historical.
- Other UI/content state failures are individually anchored in AUDIT.md. Complete device occurrence is unknown.

Proposed intervention and sole seven-day metric are in AUDIT.md. No implementation is authorized by this investigation. Runtime checks are isolated fixtures and cannot send real messages. Root owns process execution and result inspection.

Route limitations: Astral configured Sol High preflight reported Astra Ultra mismatch. No Astral exact-route success claimed. Native available independent agents performed framing, public evidence, process audits and challenge. Superpowers debugging/verification and Evidence-Led Five Whys boundaries were applied. Codex Process Jobs owns the finite verification job; launch-turn polling is prohibited. Upload skill read, no package upload action requested.

Verification: plugin typecheck and 709 plugin tests passed; focused Python tests passed 303. Initial gallery rejected because an orphaned native simulator supplied the same Overview frame for every state. The corrected native-process gallery is complete and all twenty-six captures were inspected. Twenty-four show the intended layouts. A focused temporary-copy diagnostic reproduced native NEWS list rejection (false), menu closure and return to Overview; the article was never reached. Exact native rejection reason and physical firmware outcome remain unverified. No product change was made.
