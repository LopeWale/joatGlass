#!/usr/bin/env bash
# Clone/update all Tier-1 starter repos into vendor/ (see docs/STARTER_REPOS.md)
set -euo pipefail
cd "$(dirname "$0")/../vendor"
repos=(
  gpsnmeajp/men-g2-ble-gateway
  i-soxi/even-g2-protocol
  alecnielsen/adversarial-review
  Liyan06/MiniCheck
  argmaxinc/WhisperKit
  senstella/parakeet-mlx
  google/liblc3
)
for r in "${repos[@]}"; do
  d="${r##*/}"
  if [ -d "$d/.git" ]; then git -C "$d" pull --ff-only; else git clone --depth 1 "https://github.com/$r.git" "$d"; fi
done
echo "Done. Tier-2/3 repos: clone on demand per docs/STARTER_REPOS.md"
