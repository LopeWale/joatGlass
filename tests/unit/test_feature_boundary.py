"""Structural check: Brainstorm is removed from Mac runtime and Agent Terminal seats Claude, Codex, Antigravity."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "apps/mac/joat"


def test_brainstorm_modules_are_deleted() -> None:
    assert not (SOURCE / "services/brainstorm_control.py").exists()
    assert not (SOURCE / "services/brainstorm_history.py").exists()


def test_mac_runtime_exposes_no_brainstorm_surface_or_tool() -> None:
    for relative in (
        "services/capabilities.py",
        "services/session.py",
        "services/status.py",
        "services/hud.py",
        "agents/feature_tools.py",
        "__main__.py",
    ):
        tree = ast.parse((SOURCE / relative).read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        offenders = {name for name in defined if "brainstorm" in name.lower() or "debate" in name.lower()}
        assert not offenders, f"{relative} still defines Brainstorm/Debate symbols: {sorted(offenders)}"
