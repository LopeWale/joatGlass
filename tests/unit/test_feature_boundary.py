"""Brainstorm and Agent Terminal are different features. Keep them that way.

Every other test here exercises a class in isolation, so the boundary
could be — and was — restated correctly in one module while a second
module quietly enforced the opposite. The capability view kept refusing to
open Brainstorm unless the Hermes terminal was ready for twenty commits
after the availability check stopped caring, because nothing checked the
rule ACROSS modules.

These are structural tests. They read the source rather than run it, so
they hold even where the behaviour needs a gateway, a model, or a socket
this environment does not have.

The rule, from docs/BRAINSTORM.md and the wearer's ruling:

    Brainstorm's collaborators are the Codex and Claude CLIs.
    Hermes/OpenClaw are Agent Terminal workers.
    They neither participate in a Brainstorm nor gate one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "apps/mac/joat"

WORKER_WORDS = re.compile(r"hermes|openclaw", re.IGNORECASE)
COLLABORATOR_WORDS = re.compile(r"\bcodex\b|\bclaude\b", re.IGNORECASE)


def read(relative: str) -> str:
    return (SOURCE / relative).read_text()


def _function(module: str, name: str) -> ast.AST:
    for node in ast.walk(ast.parse(read(module))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {module}")


def code_identifiers(node: ast.AST) -> str:
    """Every name, attribute and string literal a function actually USES.

    Deliberately AST-based: comments and docstrings are not in the tree, so
    prose EXPLAINING the boundary ("this must not gate on Hermes") cannot
    be mistaken for prose violating it. A first version scanned raw source
    and flagged its own explanation.
    """
    parts: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            parts.append(child.id)
        elif isinstance(child, ast.Attribute):
            parts.append(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            parts.append(child.value)
    return " ".join(parts)


def function_source(module: str, name: str) -> str:
    return code_identifiers(_function(module, name))


class TestBrainstormSeats:
    def test_the_round_table_seats_only_the_two_clis(self) -> None:
        seated = function_source("__main__.py", "current_seated")
        assert not WORKER_WORDS.search(seated), (
            "a worker runtime is being seated in the round table:\n" + seated
        )

    def test_availability_is_a_claim_about_the_clis(self) -> None:
        available = function_source("__main__.py", "brainstorm_available")
        assert not WORKER_WORDS.search(available), (
            "Brainstorm availability is gated on a worker runtime:\n" + available
        )
        assert "current_seated" in available, (
            "availability must be a claim about the seats themselves"
        )

    def test_the_seat_list_names_codex_and_claude(self) -> None:
        tree = ast.parse(read("__main__.py"))
        seats = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "seats"
        ]
        assert seats, "the seat list is gone"
        block = code_identifiers(seats[0])
        assert COLLABORATOR_WORDS.search(block), block
        assert not WORKER_WORDS.search(block), block


class TestBrainstormSurfaces:
    def test_opening_the_row_does_not_gate_on_a_worker(self) -> None:
        # Reading terminal STATE for chrome is fine; refusing to open is not.
        show = function_source("services/capabilities.py", "_show_brainstorm")
        assert "required for Brainstorm" not in show
        assert "Tap to retry" not in show

    def test_no_surface_calls_a_worker_a_collaborator(self) -> None:
        for module in ("services/capabilities.py", "services/sinks.py",
                       "services/session.py"):
            text = read(module)
            for phrase in ("Hermes participates", "Hermes is available as a Brainstorm",
                           "Hermes is required for Brainstorm"):
                assert phrase not in text, f"{module} still says: {phrase}"


class TestAgentTerminalStaysOut:
    def test_the_round_table_module_never_imports_a_worker(self) -> None:
        for module in ("services/roundtable.py",):
            tree = ast.parse(read(module))
            imported = [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            ] + [
                node.module or ""
                for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            ]
            assert not any(WORKER_WORDS.search(name) for name in imported), (
                f"{module} imports a worker runtime"
            )

    def test_the_terminal_never_imports_a_collaborator_cli(self) -> None:
        # Codex and Claude do not belong in Agent Terminal.
        tree = ast.parse(read("services/agent_terminal.py"))
        names = [
            (node.module or "") + " " + " ".join(a.name for a in node.names)
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        assert not any("subprocess_agent" in n for n in names), (
            "Agent Terminal imports the Codex/Claude CLI wrapper"
        )
        # Docstrings say Codex/Claude do NOT belong here; that is the rule
        # being stated, not broken. Only executable identifiers count.
        module = ast.parse(read("services/agent_terminal.py"))
        used = " ".join(
            child.id if isinstance(child, ast.Name) else child.attr
            for child in ast.walk(module)
            if isinstance(child, (ast.Name, ast.Attribute))
        )
        assert not COLLABORATOR_WORDS.search(used), (
            "Agent Terminal code references a Brainstorm collaborator"
        )


class TestDocumentedRule:
    def test_the_docs_still_state_the_boundary(self) -> None:
        # A doc that drifts from the code teaches the wrong rule to whoever
        # reads it next — which is how the contradiction survived before.
        brainstorm = (SOURCE.parents[2] / "docs/BRAINSTORM.md").read_text()
        assert "do not participate" in brainstorm
        assert "do not gate availability" in brainstorm
