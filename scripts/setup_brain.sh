#!/usr/bin/env bash
# setup_brain.sh — stand up the JOAT Mac brain on any Mac, idempotently.
#
#   git clone <repo> && cd <repo> && ./scripts/setup_brain.sh [--service] [--warm]
#
# Safe to re-run: every step checks before it acts. Flags:
#   --service   install + start a launchd agent so the brain runs at login
#               and restarts if it dies (the always-on Mac Studio mode)
#   --warm      pre-download the Whisper model now (~1.5 GB) instead of on
#               the first utterance
#
# What it does NOT do: edit the plugin's app.json whitelist. Adding a new
# Mac's hostname there is a deliberate security decision — this script only
# TELLS you when the current machine isn't whitelisted.

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
say "installing jarvis package (+dev tools)"
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

# ── 7. Whitelist check (the silent-failure trap) ────────────────────────────
HOSTNAME_LOCAL="$(scutil --get LocalHostName 2>/dev/null || hostname -s).local"
MANIFEST="$REPO_DIR/apps/hub-plugin/app.json"
if [ -f "$MANIFEST" ]; then
  if ! grep -qi "$HOSTNAME_LOCAL" "$MANIFEST" && ! grep -q "$(hostname -s)" "$MANIFEST"; then
    warn "this Mac ($HOSTNAME_LOCAL) is NOT in $MANIFEST's network whitelist."
    warn "The Hub blocks unlisted hosts SILENTLY — add ws://$HOSTNAME_LOCAL:8787 (or"
    warn "this machine's tailnet name) to the whitelist AND VITE_MAC_WS_URL, then"
    warn "rebuild the plugin. See docs/HUB_PLUGIN.md."
  else
    say "hostname appears in the plugin whitelist"
  fi
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
    <string>jarvis</string>
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
  say "launchd agent installed: $PLIST_LABEL (logs: $LOG_DIR/brain.log)"
  say "stop with: launchctl unload $PLIST_PATH"
else
  say "run the brain:  $VENV/bin/python -m jarvis"
  say "(or re-run with --service to keep it always on)"
fi

say "done."
