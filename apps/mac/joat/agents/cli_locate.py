"""Where the subscription CLIs actually live on this Mac.

Resolved at runtime, never cached at import: both CLIs move under us. The
2026-10-03 ChatGPT app update moved its bundled Codex from
``Resources/codex`` to ``Resources/codex-cli/bin/codex``, which left the
wearer's ``~/.local/bin/codex`` symlink dangling — ``shutil.which`` reports
nothing for a dangling link — and every boot after that logged "brainstorm
unavailable — missing: codex" until someone noticed.

The bundle now carries a manifest (``codex-package.json``) naming its own
entrypoint, so the lookup reads that rather than hardcoding the newest
layout and breaking on the next one.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

CODEX_ENV_OVERRIDE = "JOAT_CODEX_BIN"
"""An explicit path wins over every discovery rule (tests, odd installs)."""

CHATGPT_APP_ROOTS: tuple[Path, ...] = (
    Path("/Applications/ChatGPT.app"),
    Path.home() / "Applications/ChatGPT.app",
)

_EXTRA_CODEX_DIRS: tuple[Path, ...] = (
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path.home() / ".local/bin",
    Path.home() / ".npm-global/bin",
)


def _runnable(path: Path) -> bool:
    """A file we can exec. `is_file()` follows symlinks, so a dangling
    link — the exact failure this module exists for — is rejected."""
    return path.is_file() and os.access(path, os.X_OK)


def _bundled_codex(app: Path) -> Path | None:
    """The Codex CLI shipped inside a ChatGPT.app, newest layout first."""
    resources = app / "Contents/Resources"
    manifest = resources / "codex-cli/codex-package.json"
    try:
        entry = json.loads(manifest.read_text(encoding="utf-8")).get("entrypoint")
    except (OSError, ValueError, AttributeError):
        entry = None
    candidates: list[Path] = []
    if isinstance(entry, str) and entry and not os.path.isabs(entry):
        candidates.append(manifest.parent / entry)
    candidates += [
        resources / "codex-cli/bin/codex",
        resources / "codex",  # the pre-2026-10 layout
    ]
    for candidate in candidates:
        if _runnable(candidate):
            return candidate
    return None


def find_codex_binary(
    *,
    app_roots: Iterable[Path] = CHATGPT_APP_ROOTS,
    extra_dirs: Iterable[Path] = _EXTRA_CODEX_DIRS,
) -> str | None:
    """The Codex CLI to run, or None when this Mac genuinely has none.

    Order: explicit override, PATH, the ChatGPT app's bundled CLI, then the
    usual standalone install directories (the brain is often launched by
    the Mac app with a minimal PATH that holds none of them).
    """
    override = os.environ.get(CODEX_ENV_OVERRIDE, "").strip()
    if override and _runnable(Path(override)):
        return override
    on_path = shutil.which("codex")
    if on_path and _runnable(Path(on_path)):
        return on_path
    for app in app_roots:
        bundled = _bundled_codex(app)
        if bundled is not None:
            return str(bundled)
    for directory in extra_dirs:
        candidate = directory / "codex"
        if _runnable(candidate):
            return str(candidate)
    return None


__all__ = ["CODEX_ENV_OVERRIDE", "find_codex_binary"]
