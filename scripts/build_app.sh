#!/usr/bin/env bash
# build_app.sh — build, sign, (optionally) notarize and install JOAT.app.
#
#   ./scripts/build_app.sh [--install] [--notarize]
#
# Uses the real Xcode toolchain: xcodegen regenerates the project from the
# committed spec, xcodebuild archives it, exportArchive produces a
# Developer ID-signed app. This replaced a script that hand-assembled the
# bundle — which could not carry an asset catalog (so: no icon), could not
# apply entitlements, and could not produce an archive (so: no
# notarization, no TestFlight, no Sparkle-style updates).
#
# --notarize accepts EITHER credential form:
#
#   1) App Store Connect API key (recommended — no app-specific password,
#      no 2FA prompts, works unattended in CI, and does not break when the
#      Apple ID password changes). Create at App Store Connect > Users and
#      Access > Integrations > Keys, then export:
#        export JOAT_NOTARY_KEY=~/private_keys/AuthKey_ABC123.p8
#        export JOAT_NOTARY_KEY_ID=ABC123
#        export JOAT_NOTARY_ISSUER=<issuer uuid shown above the key list>
#
#   2) Keychain profile from an APP-SPECIFIC password (appleid.apple.com >
#      Sign-In and Security > App-Specific Passwords — the account password
#      itself returns HTTP 401):
#        xcrun notarytool store-credentials joat \
#          --apple-id you@example.com --team-id J2XU5XWJM9 --password abcd-efgh-ijkl-mnop

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SHELL_DIR="$REPO_DIR/apps/mac/JOATShell"
DIST="$REPO_DIR/dist"
ARCHIVE="$DIST/JOAT.xcarchive"
APP="$DIST/JOAT.app"
TEAM_ID="${JOAT_TEAM_ID:-J2XU5XWJM9}"
KEYCHAIN_PROFILE="${JOAT_NOTARY_PROFILE:-joat}"

say() { printf '\033[1;32m[app]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[app]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[app]\033[0m %s\n' "$*" >&2; exit 1; }

WANT_INSTALL=0; WANT_NOTARIZE=0
for arg in "$@"; do
  case "$arg" in
    --install) WANT_INSTALL=1 ;;
    --notarize) WANT_NOTARIZE=1 ;;
    *) die "unknown flag: $arg (known: --install --notarize)" ;;
  esac
done

command -v xcodegen >/dev/null || die "xcodegen missing: brew install xcodegen"

say "refreshing the app icon asset catalog"
"$REPO_DIR/.venv/bin/python" "$REPO_DIR/scripts/make_icon.py" \
  "$SHELL_DIR/Resources/Assets.xcassets/AppIcon.appiconset" >/dev/null

say "regenerating the project from project.yml"
(cd "$SHELL_DIR" && xcodegen generate >/dev/null)

rm -rf "$ARCHIVE" "$APP"
mkdir -p "$DIST"

# archive, not `build`: only the archive action honours ARCHS_STANDARD and
# produces the universal binary an Intel Mac would need.
say "archiving"
xcodebuild -project "$SHELL_DIR/JOAT.xcodeproj" -scheme JOAT \
  -configuration Release -archivePath "$ARCHIVE" archive \
  2>&1 | grep -E 'error:|ARCHIVE (SUCCEEDED|FAILED)' || true
[ -d "$ARCHIVE" ] || die "archive failed"

cat > "$DIST/ExportOptions.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>method</key><string>developer-id</string>
  <key>signingStyle</key><string>manual</string>
  <key>teamID</key><string>$TEAM_ID</string>
</dict>
</plist>
PLIST

say "exporting Developer ID app"
xcodebuild -exportArchive -archivePath "$ARCHIVE" \
  -exportOptionsPlist "$DIST/ExportOptions.plist" -exportPath "$DIST" \
  2>&1 | grep -E 'error:|EXPORT (SUCCEEDED|FAILED)' || true
[ -d "$APP" ] || die "export failed — inspect $DIST"

codesign --verify --strict "$APP" && say "signature verified (strict)"
say "architectures: $(lipo -archs "$APP/Contents/MacOS/JOAT")"

if [ "$WANT_NOTARIZE" = 1 ]; then
  # Prefer the API key when present; fall back to a keychain profile.
  NOTARY_ARGS=()
  if [ -n "${JOAT_NOTARY_KEY:-}" ]; then
    [ -f "$JOAT_NOTARY_KEY" ] || die "JOAT_NOTARY_KEY not found: $JOAT_NOTARY_KEY"
    [ -n "${JOAT_NOTARY_KEY_ID:-}" ] || die "JOAT_NOTARY_KEY_ID is required with the key"
    [ -n "${JOAT_NOTARY_ISSUER:-}" ] || die "JOAT_NOTARY_ISSUER is required with the key"
    NOTARY_ARGS=(--key "$JOAT_NOTARY_KEY" --key-id "$JOAT_NOTARY_KEY_ID"
                 --issuer "$JOAT_NOTARY_ISSUER")
    say "notarizing with App Store Connect API key $JOAT_NOTARY_KEY_ID"
  else
    xcrun notarytool history --keychain-profile "$KEYCHAIN_PROFILE" >/dev/null 2>&1 \
      || die "no usable credentials. Either export JOAT_NOTARY_KEY/_KEY_ID/_ISSUER,
       or create a profile with an APP-SPECIFIC password (the plain Apple ID
       password returns HTTP 401):
         xcrun notarytool store-credentials $KEYCHAIN_PROFILE \\
           --apple-id <id> --team-id $TEAM_ID --password abcd-efgh-ijkl-mnop"
    NOTARY_ARGS=(--keychain-profile "$KEYCHAIN_PROFILE")
    say "notarizing with keychain profile $KEYCHAIN_PROFILE"
  fi
  # ditto, not zip: preserves symlinks and metadata inside the bundle.
  ditto -c -k --keepParent "$APP" "$DIST/JOAT.zip"
  xcrun notarytool submit "$DIST/JOAT.zip" "${NOTARY_ARGS[@]}" --wait
  # Staple so Gatekeeper accepts the app on a Mac that is OFFLINE.
  xcrun stapler staple "$APP"
  spctl -a -vvv -t exec "$APP" 2>&1 | tail -2
else
  warn "not notarized — fine on this Mac, but Gatekeeper BLOCKS it on any"
  warn "other machine (macOS 15+ removed the Control-click bypass)."
  warn "Re-run with --notarize once a notary profile exists."
fi

if [ "$WANT_INSTALL" = 1 ]; then
  say "installing to /Applications"
  rm -rf /Applications/JOAT.app
  ditto "$APP" /Applications/JOAT.app
  say "installed: /Applications/JOAT.app"
else
  say "built: $APP   (--install to place it in /Applications)"
fi

say "done."
