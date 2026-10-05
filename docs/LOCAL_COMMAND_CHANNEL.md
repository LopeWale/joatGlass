# Bounded Mac engine commands

This slice adds only News refresh and retry of the existing dedicated Hermes/OpenClaw service. The Mac shell owns anonymous stdin/stdout pipes to its Python child. There is no listener, Hub hello, pairing token, or synthetic uplink frame. Ordinary Python stdout is redirected to the engine log before startup; a private duplicate carries protocol responses only.

Each launch has a fresh UUID epoch. Both sides validate the epoch, child PID, parent PID and protocol version. Python also verifies its current OS parent; Swift accepts responses only from its retained child pipe. New process identity invalidates pending requests and live capability observations.

JSON lines are bounded to 16 KiB. Requests carry UUIDs and a bounded deadline. The engine acknowledges admission as pending and then reports succeeded, refused, failed, timed_out, cancelled or superseded. Cancel/timeout is not a rollback guarantee. Late completion cannot acknowledge another request. Repeated actions supersede their previous request; a still-draining owner prevents another operation from entering it. Replay protection and outstanding work are bounded.

Capabilities contain only six boolean facts per allowlisted action: configured, available, active, denied, disconnected and pending. No endpoints, credentials, paths, feeds, titles, errors or personal content leave this projection. Availability describes whether the action can be attempted; active describes the underlying service state. Retry may be available while the remote service is disconnected or awaiting approval. Live snapshots arrive on the authenticated child channel; the existing status file is never command authority.

Implementation order: implement the Python dispatch/lifecycle boundary and Swift state machine; wire existing owners and UI; run focused protocol, owner and Hub authentication regression tests; build the signed app; exercise foreground News/retry and retain visual evidence. Source tests, running Mac observations, simulator and physical G2 evidence are reported separately. No .ehpk build or upload is part of this work.

## Validation receipts

The final focused run passed 168 Python tests across commands, News, pairing and the full Hub-gateway suite. This includes four additional cases separating valid saved service configuration from runtime-owner availability. Swift validation passed 95 XCTest plus 4 Swift Testing tests; the final 15 command tests were rerun after improving admission/completion wording. The channel and News modules pass strict mypy; main-module checking still reports the pre-existing notification `on_change` callback returning an asyncio Handle.

The Developer ID signed universal build at `dist/JOAT.app` passes strict signature verification. Binary SHA-256: `0b2967920fa49e64d276175cfda7cb4623b98854916d700c6a7d54f9b6cfdf65`. At final verification the shell PID was 17902 and its Python child PID was 17918. The shell loads the current Python source from this checkout.

With user-approved macOS Accessibility scripting, the actual foreground app was exercised:

- News refresh displayed engine admission, pending state and Cancel, then an explicit partial-completion acknowledgement. Some selected feeds were unavailable; this was not reported as full refresh success.
- Service retry displayed an explicit completion acknowledgement, with the dedicated session ready and the Gateway connected. Both runtime processes remained the same during these actions.
- Cancelling another News refresh displayed that cancellation was requested and earlier work might already have occurred. This was observed through Accessibility text; its screenshot capture failed, so it is not counted as screenshot evidence.
- Initial inspection exposed a configured/available conflation when native credentials were unavailable. The projection now retains configured=true when the runtime owner is absent. After a clean single-app relaunch, the native bridge and existing dedicated service were ready.

Screenshots, Accessibility observations, test output and `receipt.json` are retained in `build/local-command-channel/`. The News result and service result screenshots were visually inspected. The earlier native UI tool failure was overcome using the expressly approved fallback. No simulator or physical-G2 acceptance is claimed. No `.ehpk` packaging or upload occurred.

## Even Realities default News comparison

Official reference: [Even Dashboard support](https://support.evenrealities.com/hc/en-us/articles/14269247458319-Dashboard).

The built-in News widget uses language, news-source and interest preferences; automatic refresh is every three hours. News is presented in sets of five, with downward swiping loading another set. A tap expands the widget, swiping selects a story, another tap opens it, and a double-tap returns to the list.

These official materials do not identify its backend news supplier or confirm RSS. The public [SDK method reference](https://github.com/even-realities/everything-evenhub/blob/main/plugins/everything-evenhub/skills/sdk-reference/SKILL.md) and [Device APIs](https://hub.evenrealities.com/docs/build/device-apis) do not document a default-dashboard News retrieval or refresh API. This is a bounded documentation finding, not a claim about private implementation.

JOAT independently consumes RSS/Atom on the Mac through its existing NewsService: BBC, NPR and Google News topic feeds plus custom feed URLs, deduplicated into up to 20 cached headlines. Its interval is configurable (15-minute default; 20 minutes in the observed user settings), and this slice adds manual acknowledged refresh. It currently lacks Even's dedicated language/source preference interface and five-story load-more behavior. Those are comparison findings, not added scope in this channel slice. Even's documented [phone-owned Hub architecture](https://hub.evenrealities.com/docs/get-started/architecture) supports retaining the existing phone relay rather than impersonating it from the Mac.
