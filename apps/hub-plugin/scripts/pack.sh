#!/bin/zsh

set -euo pipefail

plugin_dir=${0:A:h:h}
cd "$plugin_dir"

# A deployable package must never inherit the ignored developer `.env` route.
# The caller selects the reviewed, private WSS G2 gateway explicitly; the
# validator then requires every candidate to be an exact app.json allowlist
# origin and the fixed /glasses path.
if [[ -z "${JOAT_RELEASE_WS_URLS:-}" ]]; then
  print -u2 "JOAT_RELEASE_WS_URLS is required (for example: wss://home-mac.example-tailnet.ts.net/glasses)"
  exit 64
fi
node scripts/release_candidates.mjs "$JOAT_RELEASE_WS_URLS" app.json

# Never package the shared dev `dist/` directory. Simulator acceptance can
# legitimately place synthetic PCM fixtures there while another terminal is
# running; archiving that mutable directory would leak DEV-only bytes into the
# canonical package. A fresh private directory gives the packer one immutable
# production snapshot.
pack_stage=$(mktemp -d "${TMPDIR:-/tmp}/joat-hub-pack.XXXXXX")
cleanup() {
  case "$pack_stage" in
    /tmp/joat-hub-pack.*|/var/folders/*/joat-hub-pack.*)
      find "$pack_stage" -mindepth 1 -delete
      rmdir "$pack_stage"
      ;;
    *)
      print -u2 "refusing to clean unexpected package staging path"
      return 1
      ;;
  esac
}
trap cleanup EXIT INT TERM

npm run typecheck
VITE_MAC_WS_URL="$JOAT_RELEASE_WS_URLS" ./node_modules/.bin/vite build --outDir "$pack_stage" --emptyOutDir

if find "$pack_stage" -type f -path '*/__joat-*/*' | grep -q .; then
  print -u2 "refusing to package DEV-only JOAT fixtures"
  exit 1
fi

# Keep the packaged floor reproducible when the registry is temporarily
# unreachable. SDK 0.0.14 requires Even App 2.2.9; app.json is the reviewed
# source of truth and the CLI must preserve it rather than use its stale
# bundled fallback.
evenhub pack app.json "$pack_stage" -o joat.ehpk --sdk-ver 0.0.14 --enforce-manual-version
