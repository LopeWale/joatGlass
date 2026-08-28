#!/bin/zsh

set -euo pipefail

plugin_dir=${0:A:h:h}
cd "$plugin_dir"

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
./node_modules/.bin/vite build --outDir "$pack_stage" --emptyOutDir

if find "$pack_stage" -type f -path '*/__joat-*/*' | grep -q .; then
  print -u2 "refusing to package DEV-only JOAT fixtures"
  exit 1
fi

evenhub pack app.json "$pack_stage" -o joat.ehpk --sdk-ver 0.0.14
