#!/usr/bin/env bash
# setup_even_ai_door.sh — point Even AI at JOAT's mind, idempotently.
#
#   ./scripts/setup_even_ai_door.sh [--probe] [--show-token] [--tailscale-bin PATH]
#
# The brain already serves an OpenAI-shaped chat endpoint on loopback
# (apps/mac/joat/services/agent_endpoint.py, port 8788). Even AI's "Agent
# Configure" can call it, which gives the wearer Even's own wake word,
# speech-to-text and native lens rendering with JOAT's mind and tools behind
# them — no plugin page, no image tiles. This script does the Mac half:
#
#   1. checks the brain is serving the door on 127.0.0.1:8788
#   2. mounts it on the tailnet next to the glasses proxy:
#        tailscale serve --bg --https=443 --set-path=/agent http://127.0.0.1:8788
#   3. verifies the mount is present, Funnel is off, and the door answers
#   4. copies the agent token to the clipboard and prints the URL to enter
#      in Even → Settings → Even AI → Agent Configure
#
# Flags:
#   --probe        also send one real question through the tailnet with the
#                  token (spends one mind turn; proves the full path)
#   --show-token   print the token instead of only copying it to the clipboard
#   --tailscale-bin PATH   Tailscale CLI (default: tailscale, then the app bundle)
#
# Safe to re-run. Read-only except for the Serve mount and the clipboard.
# It never prints existing Serve routes, never edits app.json, and never
# touches the glasses pairing state (docs/MAC_APP.md: do NOT `serve reset`).

set -euo pipefail

AGENT_PORT="${JOAT_AGENT_PORT:-8788}"
AGENT_PATH="/agent"
LOOPBACK="http://127.0.0.1:${AGENT_PORT}"
TOKEN_PATH="$HOME/Library/Application Support/JOAT/agent-token"
STATUS_PATH="$HOME/Library/Application Support/JOAT/status.json"
BRAIN_LOG="$HOME/Library/Logs/joat/brain.log"

say()  { printf '\033[1;32m[even-ai]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[even-ai]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[even-ai]\033[0m %s\n' "$*" >&2; exit 1; }

WANT_PROBE=0; SHOW_TOKEN=0; TS_BIN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --probe) WANT_PROBE=1 ;;
    --show-token) SHOW_TOKEN=1 ;;
    --tailscale-bin) shift; TS_BIN="${1:-}"; [ -n "$TS_BIN" ] || die "--tailscale-bin needs a path" ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) die "unknown flag: $1 (known: --probe --show-token --tailscale-bin PATH)" ;;
  esac
  shift
done

[ "$(uname -s)" = "Darwin" ] || die "run this on the Mac that runs the JOAT brain"
command -v curl >/dev/null || die "curl is required"
command -v python3 >/dev/null || die "python3 is required (ships with macOS Command Line Tools)"

# ---------------------------------------------------------------- 1. the door
say "checking the brain's Even AI door on ${LOOPBACK}"
health="$(curl -sS --max-time 5 "${LOOPBACK}/health" 2>/dev/null || true)"
if [ -z "$health" ] || ! printf '%s' "$health" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") is True else 1)' 2>/dev/null; then
  warn "no answer from ${LOOPBACK}/health."
  warn "The door starts with the brain only when a mind seat exists (claude or codex signed in, or the MLX seat)."
  warn "Check: JOAT.app is running (or 'python -m joat'), it was not started with --no-agent-endpoint,"
  warn "and ${BRAIN_LOG} contains 'Even AI agent endpoint: http://127.0.0.1:${AGENT_PORT}'."
  if [ -f "$STATUS_PATH" ]; then
    seat="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("seat") or "none")' "$STATUS_PATH" 2>/dev/null || echo unknown)"
    warn "status.json seat: ${seat}"
  fi
  die "start the brain, then re-run"
fi
model="$(printf '%s' "$health" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("model",""))')"
say "door is up (seat/model label: ${model:-joat})"

# ---------------------------------------------------------------- 2. token
if [ -n "${JOAT_AGENT_TOKEN:-}" ]; then
  token="$JOAT_AGENT_TOKEN"; token_source="JOAT_AGENT_TOKEN"
elif [ -r "$TOKEN_PATH" ]; then
  token="$(tr -d '[:space:]' < "$TOKEN_PATH")"; token_source="$TOKEN_PATH"
else
  die "no agent token at ${TOKEN_PATH}: the brain writes it on first boot (0600)"
fi
[ -n "$token" ] || die "agent token is empty (${token_source})"

# The door must refuse a wrong token and accept the real one. Both checks are
# content-free: an empty message list is rejected with 400 after auth passes.
code_bad="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 -X POST \
  -H 'Authorization: Bearer not-the-token' -H 'Content-Type: application/json' \
  --data '{"messages":[]}' "${LOOPBACK}/v1/chat/completions" || true)"
code_good="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 -X POST \
  -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
  --data '{"messages":[]}' "${LOOPBACK}/v1/chat/completions" || true)"
[ "$code_bad" = "401" ] || die "door accepted a wrong token (HTTP ${code_bad}); refusing to expose it"
[ "$code_good" = "400" ] || die "door rejected the token from ${token_source} (HTTP ${code_good}); the brain may be using JOAT_AGENT_TOKEN from a different environment"
say "token from ${token_source} is the one the brain enforces"

# ---------------------------------------------------------------- 3. tailscale
if [ -z "$TS_BIN" ]; then
  if command -v tailscale >/dev/null; then TS_BIN="tailscale"
  elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then TS_BIN=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  else die "tailscale CLI not found; install Tailscale or pass --tailscale-bin"; fi
fi
host="$("$TS_BIN" status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
[ -n "$host" ] || die "tailscale is not connected on this Mac (no Self.DNSName)"

say "mounting ${AGENT_PATH} -> ${LOOPBACK} on https://${host}"
# --set-path adds a mount without replacing the root handler or /glasses.
"$TS_BIN" serve --bg --https=443 --set-path="${AGENT_PATH}" "${LOOPBACK}" >/dev/null

# Verify from the persisted Serve map. Fixed vocabulary only; the raw map is
# never echoed because it names every other service on this Mac.
verdict="$("$TS_BIN" serve status --json 2>/dev/null | python3 - "$AGENT_PATH" "$LOOPBACK" <<'PY'
import json, sys
path, proxy = sys.argv[1], sys.argv[2]
try:
    data = json.load(sys.stdin)
except Exception:
    print("status_unreadable"); sys.exit(0)
mounted = False
for web in (data.get("Web") or {}).values():
    for mount, handler in (web.get("Handlers") or {}).items():
        if mount.rstrip("/") == path and (handler.get("Proxy") or "").rstrip("/") == proxy:
            mounted = True
funnel = any((data.get("AllowFunnel") or {}).values())
print("funnel_on" if funnel else ("mounted" if mounted else "not_mounted"))
PY
)"
case "$verdict" in
  mounted) say "serve map has ${AGENT_PATH} -> ${LOOPBACK}; Funnel is off" ;;
  funnel_on) die "Tailscale Funnel is enabled on this node; the door must stay tailnet-private. Turn Funnel off and re-run" ;;
  not_mounted) die "serve status does not show ${AGENT_PATH} -> ${LOOPBACK}; run '$TS_BIN serve status' and check for an older conflicting mount" ;;
  *) die "could not read 'tailscale serve status --json'" ;;
esac

URL="https://${host}${AGENT_PATH}/v1/chat/completions"

# Through the tailnet, as the phone will: health must answer, a wrong token
# must be refused. Tailscale issues the HTTPS certificate on first use, so
# allow it a moment.
say "checking the door through the tailnet at https://${host}${AGENT_PATH}/health"
tn_ok=0
for _ in 1 2 3 4 5 6; do
  if curl -sS --max-time 10 "https://${host}${AGENT_PATH}/health" 2>/dev/null | grep -q '"ok"'; then tn_ok=1; break; fi
  sleep 2
done
[ "$tn_ok" = 1 ] || die "the door does not answer over the tailnet yet; check 'tailscale serve status' and that port 443 is served"
tn_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 -X POST \
  -H 'Authorization: Bearer not-the-token' -H 'Content-Type: application/json' \
  --data '{"messages":[]}' "$URL" || true)"
[ "$tn_code" = "401" ] || die "tailnet door answered HTTP ${tn_code} to a wrong token (expected 401)"
say "tailnet door answers and enforces the token"

# ---------------------------------------------------------------- 4. probe
if [ "$WANT_PROBE" = 1 ]; then
  say "sending one real question through the tailnet (this spends a mind turn)"
  reply="$(curl -sS --max-time 90 -X POST -H "Authorization: Bearer ${token}" \
    -H 'Content-Type: application/json' \
    --data '{"model":"joat","messages":[{"role":"user","content":"Reply with the single word ready."}]}' \
    "$URL" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("choices") or [{}])[0].get("message",{}).get("content","").strip()[:80])' 2>/dev/null || true)"
  [ -n "$reply" ] || die "the probe got no reply; see ${BRAIN_LOG}"
  say "probe reply: ${reply}"
fi

# ---------------------------------------------------------------- 5. hand-off
if command -v pbcopy >/dev/null; then
  printf '%s' "$token" | pbcopy
  say "agent token copied to the clipboard (from ${token_source})"
fi
token_hint="paste from the clipboard"
if [ "$SHOW_TOKEN" = 1 ]; then say "token: ${token}"; token_hint="paste from the clipboard, or the line above"; fi

cat <<EOF

Now on the phone (Tailscale connected, same tailnet):
  Even app → Settings → Even AI → Agent Configure → Add
    Name:   JOAT
    URL:    ${URL}
    Token:  (${token_hint})
  Select JOAT as the active AI, then say: "Hey Even, what time is it in Lagos?"

Even owns the wake word, speech-to-text and lens rendering on this path;
the brain owns the mind and the tools. Actions that need the tap
confirmation (send a message, write a calendar entry) stay on the JOAT
plugin path.
EOF
