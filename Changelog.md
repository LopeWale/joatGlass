# Changelog

## 2026-08-26

- Repaired verbose WebSocket logging so pairing tokens are redacted from both
  authenticated request URLs and token-bearing pairing offer/ack frames.
- Added a real-gateway regression covering the TOFU offer and acknowledgment,
  authenticated reconnect, unchanged unauthenticated `401`, and captured-log
  token absence.
- Verification: `42 passed` in `tests/integration/test_hub_gateway.py`, `15
  passed` in `tests/unit/test_pairing.py`, and strict mypy completed with no
  issues across 39 source files.
