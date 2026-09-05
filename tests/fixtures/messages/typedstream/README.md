# Public Messages archive fixtures

These six unmodified binary fixtures come from [ReagentX/imessage-exporter](https://github.com/ReagentX/imessage-exporter/tree/4d90fc8d20a745c0a8acc1e01c0631c4bab89cb4/imessage-database/test_data/typedstream), commit `4d90fc8d20a745c0a8acc1e01c0631c4bab89cb4`. `sha256.json` records their source hashes. `LICENSE.upstream` retains the upstream GPL license. This directory contains public test material, never personal Messages database records. No upstream parser implementation is copied into JOAT.

`expected.json` is independent of JOAT and pytypedstream output:

- The two short bodies, emoji text and multipart text match the literals in upstream [body.rs](https://github.com/ReagentX/imessage-exporter/blob/4d90fc8d20a745c0a8acc1e01c0631c4bab89cb4/imessage-database/src/tables/messages/body.rs): `can_get_message_body_simple`, `can_get_message_body_simple_two`, `can_get_message_body_text_emoji`, and `can_get_message_body_multi_part`.
- The multipart expectation retains each U+FFFC attachment placeholder. Text recovery does not claim to render the attachments.
- For `LongMessage`, upstream [streamtyped.rs:220](https://github.com/ReagentX/imessage-exporter/blob/4d90fc8d20a745c0a8acc1e01c0631c4bab89cb4/imessage-database/src/util/streamtyped.rs#L220) asserts the prefix `Sed nibh velit,` and length 2359 bytes. Its complete expected string was copied from that exact public fixture's 2359-byte NSString payload, with the following end-of-object marker checked. The upstream test does not assert the full string; this snapshot does.
- `Blank` contains a zero-length NSString. Empty text is distinct from a decoding failure.

The runtime parser is the unmodified dependency `pytypedstream==0.1.0`, published under LGPL-3.0-or-later with its own distribution notices. The format evidence here comes from reverse engineering, not an Apple API guarantee.
