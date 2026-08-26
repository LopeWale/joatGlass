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
# --notarize needs a stored credential profile, created once with:
#   xcrun notarytool store-credentials joat --apple-id you@example.com \
#       --team-id TEAMID --password <app-specific-password>

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
  xcrun notarytool history --keychain-profile "$KEYCHAIN_PROFILE" >/dev/null 2>&1 \
    || die "no notary profile '$KEYCHAIN_PROFILE' — see this script's header"
  say "notarizing (waits on Apple)"
  # ditto, not zip: preserves symlinks and metadata inside the bundle.
  ditto -c -k --keepParent "$APP" "$DIST/JOAT.zip"
  xcrun notarytool submit "$DIST/JOAT.zip" \
    --keychain-profile "$KEYCHAIN_PROFILE" --wait
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
