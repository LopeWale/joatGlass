# Install the Collab System

I've placed the installation files in `collab-install/`. Do everything below in order without stopping.

## Install

1. `mkdir -p .collab/specs .collab/reports .claude/bin .claude/commands`
2. Read `collab-install/codex-bridge.sh` and save it to `.claude/bin/codex-bridge.sh` — preserve the content exactly as-is, especially bash variables. Then `chmod +x .claude/bin/codex-bridge.sh`
3. Read `collab-install/collab.md` and save it to `.claude/commands/collab.md`
4. Read `collab-install/collab-review.md` and save it to `.claude/commands/collab-review.md`
5. Read `collab-install/CLAUDE-md-addition.md` and insert its content into `CLAUDE.md` before the last section
6. Add `.collab/` to `.gitignore` if not already there

## Verify

Run these and report results:
1. `ls -la .claude/bin/codex-bridge.sh` — must be executable
2. `head -1 .claude/commands/collab.md` — must show "/collab"
3. `head -1 .claude/commands/collab-review.md` — must show "/collab-review"
4. `grep "Cross-Model Collaboration" CLAUDE.md` — must find the section
5. `.claude/bin/codex-bridge.sh think "Run: echo OPENAI_API_KEY=\$OPENAI_API_KEY — report the exact output"` — API key MUST be empty. If it shows sk-*, STOP.
6. `.claude/bin/codex-bridge.sh think "Read package.json, tell me the project name. One line."` — must return the correct name
7. `.claude/bin/codex-bridge.sh build "Create .collab/test.txt containing WORKS"` — then `cat .collab/test.txt` must show WORKS, then `rm .collab/test.txt`

## Clean up

`rm -rf collab-install/`

## Report

Tell me how many of the 7 checks passed. If all passed: "Collab system installed. /collab and /collab-review are ready."
