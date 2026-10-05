#!/usr/bin/env bash
set -euo pipefail

# Run the signed, installed app with its established permissions and one engine.
# A successful compile alone does not establish that any UI control works.
MODE="${1:---verify}"
case "$MODE" in
  --verify|--logs|--telemetry|--debug|run) ;;
  *) echo "Usage: $0 [run|--verify|--logs|--telemetry|--debug]" >&2; exit 2 ;;
esac
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_BUNDLE="/Applications/JOAT.app"
APP_BINARY="$APP_BUNDLE/Contents/MacOS/JOAT"
BUILD_DIR="$ROOT_DIR/dist/mac-workspace"
mkdir -p "$BUILD_DIR"
REPLACED_PIDS=""
while read -r candidate_pid candidate_command; do
  if [[ "$candidate_command" == "$APP_BINARY" || "$candidate_command" == "$ROOT_DIR/dist/mac-workspace/Build/Products/Debug/JOAT.app/Contents/MacOS/JOAT" ]]; then
    REPLACED_PIDS="$REPLACED_PIDS $candidate_pid"
  fi
done < <(ps -axo pid=,comm=)

# The canonical build retains the previous /Applications app as a backup.
if ! "$ROOT_DIR/scripts/build_app.sh" --install >"$BUILD_DIR/build.log" 2>&1; then
  tail -60 "$BUILD_DIR/build.log"
  exit 1
fi
codesign --verify --strict "$APP_BUNDLE"
[[ "$(plutil -extract CFBundleIdentifier raw "$APP_BUNDLE/Contents/Info.plist")" == "com.joat.shell" ]]
[[ "$(plutil -extract JOATDesignPreview raw "$APP_BUNDLE/Contents/Info.plist")" == "NO" ]] \
  || { echo "Refusing to launch a read-only preview as the working app." >&2; exit 1; }

# Ask AppKit to quit gracefully, allowing JOAT to wait for its engine child.
# Match both bundle identity and executable path; leave other checkouts alone.
JOAT_REPLACED_APP_PIDS="$REPLACED_PIDS" \
/usr/bin/swift - <<'SWIFT'
import AppKit
import Foundation
let processIDs = Set(ProcessInfo.processInfo.environment["JOAT_REPLACED_APP_PIDS"]!
    .split(separator: " ").compactMap { Int32($0) })
let targets = NSWorkspace.shared.runningApplications.filter {
    processIDs.contains($0.processIdentifier) &&
    ["com.joat.shell", "com.joat.shell.debug"].contains($0.bundleIdentifier ?? "")
}
for app in targets { app.terminate() }
let deadline = Date().addingTimeInterval(15)
while targets.contains(where: { !$0.isTerminated }) && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.1))
}
guard targets.allSatisfy({ $0.isTerminated }) else {
    fputs("JOAT has not finished quitting; the new app was not launched.\n", stderr)
    exit(1)
}
SWIFT

/usr/bin/open "$APP_BUNDLE"
app_pid=""
for attempt in 1 2 3 4 5; do
  while read -r candidate_pid candidate_command; do
    if [[ "$candidate_command" == "$APP_BINARY" ]]; then app_pid="$candidate_pid"; fi
  done < <(ps -axo pid=,comm=)
  [[ -n "$app_pid" ]] && break
  sleep 1
done
[[ -n "$app_pid" ]] || { echo "JOAT did not stay running." >&2; exit 1; }
echo "Built and opened working JOAT (PID $app_pid): $APP_BUNDLE"
echo "Launch verified. Controls still require interaction and effect checks."
case "$MODE" in
  --debug) lldb -p "$app_pid" ;;
  --logs|--telemetry) /usr/bin/log stream --info --style compact --predicate "processID == $app_pid" ;;
esac
