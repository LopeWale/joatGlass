#!/bin/zsh

set -euo pipefail

plugin_dir=${0:A:h:h}
cd "$plugin_dir"

# This packages the WORKING TREE, not a commit. That is convenient during
# development and a hazard at release: a pack run in this repo quietly
# archived another concurrent session's uncommitted, unreviewed renderer
# edits into a hardware-deployable artifact, and nothing in the output said
# so. A package is supposed to correspond to a revision someone can name.
#
# Set JOAT_RELEASE_ALLOW_DIRTY=1 to package a dirty tree deliberately (a
# release candidate mid-edit); the stamp below then says which revision it
# is based on and that it is not that revision.
if git -C "$plugin_dir" rev-parse --git-dir >/dev/null 2>&1; then
  release_rev=$(git -C "$plugin_dir" rev-parse --short HEAD)
  dirty=$(git -C "$plugin_dir" status --porcelain -- "$plugin_dir")
  if [[ -n "$dirty" ]]; then
    if [[ "${JOAT_RELEASE_ALLOW_DIRTY:-0}" != "1" ]]; then
      print -u2 "refusing to package uncommitted changes under $plugin_dir:"
      print -u2 "$dirty"
      print -u2 "commit them, or set JOAT_RELEASE_ALLOW_DIRTY=1 to override."
      exit 65
    fi
    print -u2 "WARNING: packaging a DIRTY tree based on $release_rev"
  else
    print -u2 "packaging clean tree at $release_rev"
  fi
fi

# A deployable package must never inherit the ignored developer `.env` route.
# The caller selects the reviewed, private WSS G2 gateway explicitly; the
# validator then requires every candidate to be an exact app.json allowlist
# origin and the fixed /glasses path. `release_manifest.mjs` narrows the
# packaged permission set to exactly those selected WSS origins.
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
release_stage=$(mktemp -d "${TMPDIR:-/tmp}/joat-hub-release.XXXXXX")
pack_stage="$release_stage/dist"
release_manifest="$release_stage/app.json"
mkdir -m 700 "$pack_stage"
cleanup() {
  case "$release_stage" in
    /tmp/joat-hub-release.*|/var/folders/*/joat-hub-release.*)
      find "$release_stage" -mindepth 1 -delete
      rmdir "$release_stage"
      ;;
    *)
      print -u2 "refusing to clean unexpected release staging path"
      return 1
      ;;
  esac
}
trap cleanup EXIT INT TERM

node scripts/release_manifest.mjs "$JOAT_RELEASE_WS_URLS" app.json "$release_manifest"

# A release build sets publicDir=false, so NOTHING in public/ is copied.
# That is what keeps DEV fixtures out of the bundle (see vite.config.ts) —
# and it means a genuinely shippable asset dropped into public/ would go
# missing from the package without a word. Refuse instead: everything in
# public/ must be a __joat-* fixture, or this is not a release-safe layout.
if [[ -d public ]]; then
  stray=("${(@f)$(find public -mindepth 1 -maxdepth 1 ! -name '__joat-*' 2>/dev/null)}")
  if [[ -n "${stray[1]}" ]]; then
    print -u2 "public/ holds assets a release build will NOT copy:"
    print -u2 -- "${(F)stray}"
    print -u2 "a release sets publicDir=false; import them from src/ instead."
    exit 1
  fi
fi

npm run typecheck
VITE_MAC_WS_URL="$JOAT_RELEASE_WS_URLS" ./node_modules/.bin/vite build --outDir "$pack_stage" --emptyOutDir

if find "$pack_stage" -type f -path '*/__joat-*/*' | grep -q .; then
  print -u2 "refusing to package DEV-only JOAT fixtures"
  exit 1
fi

# Keep the packaged floor reproducible when the registry is temporarily
# unreachable. SDK 0.0.16 requires Even App 2.2.10 (SDK README, 0.0.15
# changelog: "Raised the minimum supported Even App version to 2.2.10");
# app.json is the reviewed
# source of truth and the CLI must preserve it rather than use its stale
# bundled fallback.
evenhub pack "$release_manifest" "$pack_stage" -o joat.ehpk --sdk-ver 0.0.16 --enforce-manual-version
