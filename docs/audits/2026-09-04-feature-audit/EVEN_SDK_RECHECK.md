# Even SDK recheck, September 5, 2026

This checks vendor documentation against the current dirty checkout. It is not visual acceptance or physical-G2 proof. No product code changed during this check.

## Version and source authority

JOAT declares and has installed SDK 0.0.14. The live npm registry inspected during this check also reported latest 0.0.14, published August 20, 2026. The installed package README requires Even App 2.2.9+, matching JOAT's manifest; the actual phone application version was not inspected.

Even's [GitHub SDK reference](https://github.com/even-realities/everything-evenhub/blob/main/plugins/everything-evenhub/skills/sdk-reference/SKILL.md) still labels itself 0.0.12. For additions in 0.0.14, use the installed vendor [README](../../../apps/hub-plugin/node_modules/@evenrealities/even_hub_sdk/README.md) and [type declarations](../../../apps/hub-plugin/node_modules/@evenrealities/even_hub_sdk/dist/index.d.ts), not that older version headline. The README adds brightness control, contextual menus, long-press events and processed audio metadata. Brightness is not font sizing.

## What the display documentation establishes

Even specifies a 576x288 canvas, at most four 288x144 image containers, and exactly one input capture container. Native text has no font-size control. Custom lettering therefore needs image rendering. Nothing reserves the reader's right third. Image transfers need serialization; vendor guidance estimates 0.5–2 seconds per image frame and warns against concurrent rendering/storage bridge calls. These are platform constraints and guidance, not measured JOAT timings. [Official glasses UI reference](https://github.com/even-realities/everything-evenhub/blob/main/plugins/everything-evenhub/skills/glasses-ui/SKILL.md).

Current production custom rendering uses four tiles and a blank capture layer. The banner uses the existing canvas font: 12px clock, 10px label/mic/battery. Notes and full Messages readers span x14..562; News/Search retain x36 on the left for the tab rail. Source anchors: `HudPageBuilder.ts:219`, `HudCanvasRenderer.ts:1755`, `HudCanvasRenderer.ts:2336`, `main.ts:369`.

## Microphone boundary

The SDK supplies signed 16-bit little-endian mono PCM at 16 kHz. Glasses audio requires successful startup; phone audio does not. JOAT must still perform transcription and connect the resulting conversation to Messages. The documented SDK surface supplies neither JOAT's Mac Messages integration nor a speech-to-text service. Current AudioStreamer preserves the startup guard. [Official device features reference](https://github.com/even-realities/everything-evenhub/blob/main/plugins/everything-evenhub/skills/device-features/SKILL.md).

## Unresolved source gaps, not demonstrated incident causes

- `main.ts:272–273` and `820–821` issue parallel fire-and-forget SDK storage writes. This disagrees with the vendor's all-call serialization guidance; no connection-crash reproduction was performed here.
- `HudVisualSurface.ts:222` attributes a 100ms image hold window to Even. That statement was not found in the current references inspected. The 110ms pacing remains unchanged pending evidence; it must not be presented as a verified current vendor requirement.
- `main.ts:829–831` tears down before requesting the SDK's confirmation-mode shutdown. Cancellation behavior needs a reproducer before repair.
- If custom visual startup fails, `main.ts:346` permits native fallback. That path retains `HudPageBuilder.ts:303`'s 46-character footer cap. Neither its activation in the reported incident nor a new regression is established.

## Immediate acceptance work

The Mac is accessible again. A persistent interactive mode in the existing isolated simulator harness supports direct inspection of current-source News, Messages and Notes, retaining source hashes before fixture instrumentation. Synthetic adapters, disabled fixture audio and normalized simulator Back remain explicit limitations. New feature expansion is paused while these visible failures are checked.
