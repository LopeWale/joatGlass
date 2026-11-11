#!/usr/bin/env bash
# build_ios.sh — build JOAT Brain and install it on the lab iPhone.
#
#   ./scripts/build_ios.sh [--install] [--launch] [--device <udid-or-name>]
#   ./scripts/build_ios.sh --testflight
#
# --testflight archives a Release build and uploads it to App Store Connect
# (app "JOAT Brain Lab", com.joat.brain) for TestFlight, so the phone can
# update away from this Mac. It signs in with the App Store Connect API key
# in ~/.appstoreconnect/private_keys: JOAT_ASC_KEY_ID and JOAT_ASC_ISSUER_ID
# come from the environment or the repo's ignored .env (only those two
# lines are read). The build number is the upload time.
#
# Same shape as build_app.sh: xcodegen regenerates the project from the
# committed spec, xcodebuild builds a Debug app signed with the builder's
# own team (JOAT_TEAM_ID, environment or the repo's ignored .env), and
# `xcrun devicectl` installs it over the cable or Wi-Fi pairing. A Debug
# development build is what a lab device needs: it carries get-task-allow
# so Console.app / `log stream --device` can follow it.
#
# The phone must have Developer Mode on (Settings > Privacy & Security >
# Developer Mode) and be paired with this Mac (Xcode > Window > Devices).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$REPO_DIR/apps/ios-companion"
DERIVED="$REPO_DIR/dist/ios-derived"
APP="$DERIVED/Build/Products/Debug-iphoneos/JOATBrain.app"
BUNDLE_ID="com.joat.brain"
# Read only this one key from .env; never source it (it also holds API keys).
TEAM_ID="${JOAT_TEAM_ID:-$(sed -n 's/^JOAT_TEAM_ID=//p' "$REPO_DIR/.env" 2>/dev/null | tail -n 1)}"

say() { printf '\033[1;32m[ios]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[ios]\033[0m %s\n' "$*" >&2; exit 1; }

WANT_INSTALL=0; WANT_LAUNCH=0; WANT_TESTFLIGHT=0; DEVICE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --install) WANT_INSTALL=1 ;;
    --launch) WANT_INSTALL=1; WANT_LAUNCH=1 ;;
    --device) shift; DEVICE="${1:-}" ;;
    --testflight) WANT_TESTFLIGHT=1 ;;
    *) die "unknown flag: $1 (known: --install --launch --device <udid-or-name> --testflight)" ;;
  esac
  shift
done

command -v xcodegen >/dev/null || die "xcodegen missing: brew install xcodegen"
[ -n "$TEAM_ID" ] || die "set JOAT_TEAM_ID (your Apple Developer team ID) in the environment or the repo's .env"
export JOAT_TEAM_ID="$TEAM_ID"   # project.yml reads it as \${JOAT_TEAM_ID}

# The wearer's News choice from the Mac app, as the phone's starting point
# (the phone keeps its own copy once it has one: Documents/news.json).
mkdir -p "$APP_DIR/Generated"
NEWS_CONFIG="$HOME/Library/Application Support/JOAT/news.json"
if [ -f "$NEWS_CONFIG" ]; then cp "$NEWS_CONFIG" "$APP_DIR/Generated/news-config.json"; fi
# The link to this Mac's assistant: its tailnet address (Tailscale Serve
# carries /agent to the agent endpoint) and the endpoint's bearer token.
AGENT_TOKEN_FILE="$HOME/Library/Application Support/JOAT/agent-token"
TS_HOST="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
if [ -n "$TS_HOST" ] && [ -f "$AGENT_TOKEN_FILE" ]; then
  TS_HOST="$TS_HOST" TOKEN_FILE="$AGENT_TOKEN_FILE" OUT="$APP_DIR/Generated/mac-link.json" python3 - <<'PY_LINK'
import json, os
token = open(os.environ["TOKEN_FILE"]).read().strip()
url = f"https://{os.environ['TS_HOST']}/agent/v1/chat/completions"
with open(os.environ["OUT"], "w") as f:
    json.dump({"url": url, "token": token}, f)
PY_LINK
  say "mac link: https://$TS_HOST/agent"
else
  say "no mac link (tailscale or the agent token missing): answers will say the Mac is unreachable"
fi

# Claude, asked directly by the phone (the wearer's choice, 2026-10-23):
# only ANTHROPIC_API_KEY is read from .env, never the whole file.
CLAUDE_KEY="${ANTHROPIC_API_KEY:-$(sed -n 's/^ANTHROPIC_API_KEY=//p' "$REPO_DIR/.env" 2>/dev/null | tail -n 1)}"
if [ -n "$CLAUDE_KEY" ]; then
  CLAUDE_KEY="$CLAUDE_KEY" OUT="$APP_DIR/Generated/claude.json" python3 -c 'import json, os; json.dump({"key": os.environ["CLAUDE_KEY"].strip().strip(chr(34) + chr(39))}, open(os.environ["OUT"], "w"))'
  say "claude: answers and note summaries go straight to Claude"
else
  rm -f "$APP_DIR/Generated/claude.json"
  say "no ANTHROPIC_API_KEY: answers and summaries go to the Mac"
fi

say "regenerating the project from project.yml"
(cd "$APP_DIR" && xcodegen generate >/dev/null)

if [ "$WANT_TESTFLIGHT" = 1 ]; then
  ASC_KEY_ID="${JOAT_ASC_KEY_ID:-$(sed -n 's/^JOAT_ASC_KEY_ID=//p' "$REPO_DIR/.env" 2>/dev/null | tail -n 1)}"
  ASC_ISSUER="${JOAT_ASC_ISSUER_ID:-$(sed -n 's/^JOAT_ASC_ISSUER_ID=//p' "$REPO_DIR/.env" 2>/dev/null | tail -n 1)}"
  ASC_KEY="$HOME/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8"
  [ -n "$ASC_KEY_ID" ] && [ -n "$ASC_ISSUER" ] || die "set JOAT_ASC_KEY_ID and JOAT_ASC_ISSUER_ID (environment or .env)"
  [ -f "$ASC_KEY" ] || die "no API key at $ASC_KEY"
  AUTH=(-authenticationKeyPath "$ASC_KEY" -authenticationKeyID "$ASC_KEY_ID" -authenticationKeyIssuerID "$ASC_ISSUER")
  BUILD_NUMBER="$(date +%Y%m%d%H%M)"
  ARCHIVE="$REPO_DIR/dist/ios-archive/JOATBrain-$BUILD_NUMBER.xcarchive"
  EXPORT="$REPO_DIR/dist/ios-export/$BUILD_NUMBER"
  mkdir -p "$EXPORT"
  say "archiving Release build $BUILD_NUMBER"
  xcodebuild -project "$APP_DIR/JOATBrain.xcodeproj" -scheme JOATBrain \
    -configuration Release -destination "generic/platform=iOS" \
    -archivePath "$ARCHIVE" -allowProvisioningUpdates "${AUTH[@]}" \
    CURRENT_PROJECT_VERSION="$BUILD_NUMBER" archive \
    2>&1 | grep -E 'error:|BUILD|ARCHIVE (SUCCEEDED|FAILED)' || true
  [ -d "$ARCHIVE" ] || die "archive failed — run the xcodebuild line without the grep to see why"
  cat > "$EXPORT/ExportOptions.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>destination</key><string>upload</string>
  <key>teamID</key><string>$TEAM_ID</string>
  <key>signingStyle</key><string>automatic</string>
  <key>uploadSymbols</key><true/>
  <key>manageAppVersionAndBuildNumber</key><false/>
</dict></plist>
PLIST
  say "uploading to App Store Connect"
  xcodebuild -exportArchive -archivePath "$ARCHIVE" -exportPath "$EXPORT" \
    -exportOptionsPlist "$EXPORT/ExportOptions.plist" -allowProvisioningUpdates "${AUTH[@]}" \
    > "$EXPORT/export.log" 2>&1 || true
  grep -E '^error|Uploaded|EXPORT (SUCCEEDED|FAILED)' "$EXPORT/export.log" || true
  grep -q 'EXPORT SUCCEEDED' "$EXPORT/export.log" || die "upload failed (see $EXPORT/export.log)"
  say "uploaded build $BUILD_NUMBER; TestFlight lists it once Apple finishes processing (usually 5-15 minutes)"
  exit 0
fi

# Pick the phone: the only connected iPhone, or the one named with --device.
pick_device() {
  local json
  json="$(xcrun devicectl list devices --json-output /dev/stdout 2>/dev/null || true)"
  [ -n "$json" ] || return 1
  JSON="$json" DEVICE="$DEVICE" python3 - <<'PY'
import json, os, sys
doc = json.loads(os.environ["JSON"])
want = os.environ["DEVICE"]
rows = []
for d in doc.get("result", {}).get("devices", []):
    props = d.get("deviceProperties", {})
    hw = d.get("hardwareProperties", {})
    conn = d.get("connectionProperties", {})
    name = props.get("name", "")
    udid = hw.get("udid", "")
    plat = hw.get("platform", "")
    state = conn.get("pairingState", "") + "/" + conn.get("tunnelState", "")
    if plat != "iOS":
        continue
    if want and want not in (name, udid, d.get("identifier", "")):
        continue
    rows.append((d.get("identifier", ""), name, udid, state))
if not rows:
    sys.exit(1)
if len(rows) > 1 and not want:
    for r in rows:
        print(f"  {r[1]}  {r[0]}  {r[3]}", file=sys.stderr)
    print("several iPhones; pick one with --device", file=sys.stderr)
    sys.exit(2)
print(rows[0][0])
PY
}

DEVICE_ID="$(pick_device)" || die "no paired iPhone found (xcrun devicectl list devices). Plug it in, trust this Mac, and enable Developer Mode."
say "device: $DEVICE_ID"

say "building Debug for the device"
# Never leave an older build behind to be installed when this one fails
# (2026-10-23: the phone dropped off, the build failed, and the previous
# app was about to be installed in its place).
rm -rf "$APP"
xcodebuild -project "$APP_DIR/JOATBrain.xcodeproj" -scheme JOATBrain \
  -configuration Debug -destination "generic/platform=iOS" \
  -derivedDataPath "$DERIVED" -allowProvisioningUpdates build \
  2>&1 | grep -E 'error:|warning: .*(entitlement|provisioning)|BUILD (SUCCEEDED|FAILED)' || true
[ -d "$APP" ] || die "build failed — run the xcodebuild line without the grep to see why"
say "built: $APP"

if [ "$WANT_INSTALL" = 1 ]; then
  say "installing on the phone"
  xcrun devicectl device install app --device "$DEVICE_ID" "$APP"
fi
if [ "$WANT_LAUNCH" = 1 ]; then
  say "launching $BUNDLE_ID"
  xcrun devicectl device process launch --device "$DEVICE_ID" --terminate-existing "$BUNDLE_ID"
fi
say "done. Follow it in Console.app (pick the iPhone, filter subsystem com.joatlabs.joatbrain), or snapshot with: log collect --device-udid <udid> --last 10m"
