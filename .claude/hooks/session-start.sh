#!/bin/bash
# SessionStart hook: give a Claude Code cloud session the Even Realities G2
# skills (everything-evenhub) that CLAUDE.md relies on. The container is
# fresh each session, so the plugin enabled in .claude/settings.json has to
# be fetched again; idempotent, nothing to do once it is present.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi
if ! command -v claude >/dev/null 2>&1; then
  echo "claude CLI not on PATH; skipping the everything-evenhub install"
  exit 0
fi

MARKETPLACE="everything-evenhub"
PLUGIN="everything-evenhub@everything-evenhub"
INSTALLED="${HOME}/.claude/plugins/installed_plugins.json"

if ! claude plugin marketplace list 2>/dev/null | grep -q "^  > ${MARKETPLACE}\$"; then
  claude plugin marketplace add even-realities/everything-evenhub
fi
if ! grep -q "\"${PLUGIN}\"" "${INSTALLED}" 2>/dev/null; then
  claude plugin install "${PLUGIN}"
fi
echo "everything-evenhub skills ready"
