#!/usr/bin/env bash
# Run the JOAT Hub plugin on the REAL glasses via Even's dev-mode QR flow.
#
# One terminal, one command: starts the Vite dev server on the LAN and prints
# a QR code. Scan it from the Even app's Hub tab (Developer Mode must be
# enabled once — docs/HUB_PLUGIN.md "On-device runbook"). Edits hot-reload
# onto the glasses without re-scanning.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN="$ROOT/apps/hub-plugin"
PORT=5190   # pinned in vite.config.ts (strictPort)
EVENHUB="${EVENHUB:-$HOME/.local/bin/evenhub}"

command -v "$EVENHUB" >/dev/null 2>&1 || {
  echo "evenhub CLI not found at $EVENHUB" >&2
  echo 'install: npm install -g --prefix="$HOME/.local" @evenrealities/evenhub-cli' >&2
  exit 1
}

# The phone loads the page from this address, so it must be the LAN IP —
# localhost inside the QR would point the phone at itself.
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
[ -n "$IP" ] || { echo "no LAN IP on en0/en1 — is Wi-Fi up?" >&2; exit 1; }

# The plugin renders without the engine, but voice/cards need it: warn, don't block.
if ! lsof -nP -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "WARNING: nothing listening on :8787 — start JOAT.app (or .venv/bin/python -m jarvis)"
  echo "         The HUD page will render, but voice and cards will be dead."
fi
[ -f "$PLUGIN/.env" ] || \
  echo "WARNING: apps/hub-plugin/.env missing — MacLink will use the wss://mac.local fallback."

echo "dev server: http://$IP:$PORT  (phone and Mac must share this network)"
( cd "$PLUGIN" && npm run dev -- --host ) &
VITE_PID=$!
trap 'kill "$VITE_PID" 2>/dev/null || true' EXIT INT TERM

for _ in $(seq 1 40); do
  nc -z 127.0.0.1 "$PORT" 2>/dev/null && break
  sleep 0.25
done
nc -z 127.0.0.1 "$PORT" 2>/dev/null || { echo "vite never opened :$PORT" >&2; exit 1; }

echo
echo "Scan from the Even app: Even Hub tab -> developer section (top right) -> Scan QR"
"$EVENHUB" qr --url "http://$IP:$PORT"
echo
echo "Leave this running. Ctrl-C stops the dev server."
wait "$VITE_PID"
