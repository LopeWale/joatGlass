#!/usr/bin/env bash
# setup_brain.sh — prepare JOAT's Python core on any Mac, idempotently.
#
#   git clone <repo> && cd <repo> && ./scripts/setup_brain.sh [--service] [--warm]
#
# Safe to re-run: every step checks before it acts. Flags:
#   --service   install + start a *legacy core-only* launchd agent. It runs
#               Python without JOAT.app's native bridge, so it cannot host
#               Keychain-backed Agent Terminal/Hermes. Use JOAT.app's
#               Launch at Login for the unattended glasses-first home brain.
#   --warm      pre-download the Whisper model now (~1.5 GB) instead of on
#               the first utterance
#
# What it does NOT do: start JOAT.app's native bridge or edit the plugin's
# release URLs. Adding a Mac the plugin may dial is a deliberate security
# decision — this script only TELLS you when the current machine is missing
# from JOAT_RELEASE_WS_URLS in apps/hub-plugin/.env.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/.venv"
STT_MODEL="${JOAT_STT_MODEL:-large-v3_turbo}"
PLIST_LABEL="com.joat.brain"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
LOG_DIR="$HOME/Library/Logs/joat"

say() { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; exit 1; }

WANT_SERVICE=0; WANT_WARM=0
for arg in "$@"; do
  case "$arg" in
    --service) WANT_SERVICE=1 ;;
    --warm) WANT_WARM=1 ;;
    *) die "unknown flag: $arg (known: --service --warm)" ;;
  esac
done

# ── 1. Homebrew ──────────────────────────────────────────────────────────────
if ! command -v brew >/dev/null; then
  die "Homebrew is required: https://brew.sh (one command, then re-run this)"
fi

# ── 2. Python ≥ 3.12 ────────────────────────────────────────────────────────
PY=""
for cand in python3.14 python3.13 python3.12 python3; do
  if command -v "$cand" >/dev/null; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
      PY="$(command -v "$cand")"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  say "installing python@3.12 via brew"
  brew install -q python@3.12
  PY="$(brew --prefix python@3.12)/bin/python3.12"
fi
say "python: $PY ($("$PY" --version 2>&1))"

# ── 3. venv + package ───────────────────────────────────────────────────────
if [ ! -x "$VENV/bin/python" ]; then
  say "creating venv"
  "$PY" -m venv "$VENV"
fi
say "installing joat package (+dev tools)"
"$VENV/bin/pip" install -q -e "$REPO_DIR[dev,mlx]"

# ── 4. whisperkit-cli ───────────────────────────────────────────────────────
if ! command -v whisperkit-cli >/dev/null; then
  say "installing whisperkit-cli (STT, Neural Engine)"
  brew install -q whisperkit-cli
else
  say "whisperkit-cli: $(whisperkit-cli --version 2>/dev/null || echo present)"
fi

# ── 5. Optional model warmup ────────────────────────────────────────────────
if [ "$WANT_WARM" = 1 ]; then
  say "warming Whisper model '$STT_MODEL' (first-time download can take minutes)"
  WARM_WAV="$(mktemp -d)/warm.wav"
  say -o "${WARM_WAV%.wav}.aiff" "ready" 2>/dev/null || true
  if [ -f "${WARM_WAV%.wav}.aiff" ]; then
    afconvert -f WAVE -d LEI16@16000 -c 1 "${WARM_WAV%.wav}.aiff" "$WARM_WAV"
    whisperkit-cli transcribe --audio-path "$WARM_WAV" --model "$STT_MODEL" >/dev/null 2>&1 \
      && say "model warm" || warn "warmup failed — model will download on first utterance"
  fi
fi

# ── 6. Sanity: tests + types ────────────────────────────────────────────────
say "running the test suite"
"$VENV/bin/python" -m pytest -q "$REPO_DIR/tests" | tail -1

# ── 7. Plugin target check (the silent-failure trap) ────────────────────────
# A packaged plugin can reach only the Macs named in JOAT_RELEASE_WS_URLS in
# apps/hub-plugin/.env (they become its whole network whitelist).
PLUGIN_ENV="$REPO_DIR/apps/hub-plugin/.env"
SHORT_NAME="$(hostname -s)"
if ! grep -q '^JOAT_RELEASE_WS_URLS=' "$PLUGIN_ENV" 2>/dev/null; then
  warn "no JOAT_RELEASE_WS_URLS in apps/hub-plugin/.env yet — copy .env.example there,"
  warn "set your package id and this Mac's wss://<name>.<tailnet>.ts.net/glasses URL,"
  warn "then npm run pack. See apps/hub-plugin/README.md."
elif ! grep '^JOAT_RELEASE_WS_URLS=' "$PLUGIN_ENV" | grep -qi "$SHORT_NAME"; then
  warn "this Mac ($SHORT_NAME) does not appear in JOAT_RELEASE_WS_URLS — the plugin"
  warn "will not dial it. Add its Tailscale /glasses URL and repack the plugin."
else
  say "this Mac appears in the plugin's release URLs"
fi

# ── 8. Optional launchd service ─────────────────────────────────────────────
if [ "$WANT_SERVICE" = 1 ]; then
  mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
  cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PLIST_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>-m</string>
    <string>joat</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/brain.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/brain.log</string>
</dict>
</plist>
PLIST
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  launchctl load "$PLIST_PATH"
  warn "legacy core-only launchd agent installed: $PLIST_LABEL (logs: $LOG_DIR/brain.log)"
  warn "Agent Terminal and Hermes stay unavailable because JOAT.app owns their native bridge."
  warn "For the unattended glasses-first home brain, install JOAT.app and enable Launch at Login."
  say "stop with: launchctl unload $PLIST_PATH"
else
  say "run the brain:  $VENV/bin/python -m joat"
  say "(--service keeps only this legacy Python core running; see docs/MAC_APP.md for JOAT.app home-brain setup)"
fi

say "done."
