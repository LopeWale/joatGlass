#!/usr/bin/env bash
# build_app.sh — build, sign, and install JOAT.app (the native Mac shell).
#
#   ./scripts/build_app.sh [--install]
#
# Builds apps/mac/JOATShell with SwiftPM, assembles a proper .app bundle,
# signs it (Developer ID if available — stable TCC identity — else ad-hoc
# with a warning), and with --install copies it to /Applications.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SHELL_DIR="$REPO_DIR/apps/mac/JOATShell"
DIST="$REPO_DIR/dist"
APP="$DIST/JOAT.app"
BUNDLE_ID="com.joat.shell"

say() { printf '\033[1;32m[app]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[app]\033[0m %s\n' "$*"; }

say "building (swift build -c release)"
(cd "$SHELL_DIR" && swift build -c release >/dev/null)
BIN="$SHELL_DIR/.build/release/JOAT"

say "assembling bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/JOAT"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>JOAT</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>JOAT</string>
  <key>CFBundleDisplayName</key><string>JOAT</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHumanReadableCopyright</key><string>JOAT — personal assistant shell</string>
</dict>
</plist>
PLIST
printf 'APPL????' > "$APP/Contents/PkgInfo"

IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
  | grep -m1 'Developer ID Application' | sed 's/.*"\(.*\)".*/\1/')
if [ -n "${IDENTITY:-}" ]; then
  say "signing with: $IDENTITY (hardened runtime)"
  codesign --force --options runtime --sign "$IDENTITY" "$APP"
else
  warn "no Developer ID identity — ad-hoc signing (FDA grant will NOT survive rebuilds)"
  codesign --force --sign - "$APP"
fi
codesign --verify --deep "$APP" && say "signature verified"

if [ "${1:-}" = "--install" ]; then
  say "installing to /Applications (replacing any old copy)"
  rm -rf /Applications/JOAT.app
  cp -R "$APP" /Applications/JOAT.app
  say "installed: /Applications/JOAT.app"
else
  say "built: $APP   (run with --install to copy to /Applications)"
fi
