"""Tools the model can call to GO GET an answer.

The wearer's ruling, verbatim in spirit: an assistant that answers "I don't
know" and stops is as fired as one that makes the answer up. There are three
behaviors — confabulate, decline, or go figure out what is needed and get
it — and only the third is an assistant. It is also the entire reason an
LLM sits here at all: deterministic code only handles what someone
anticipated; the model's job is finding the path nobody enumerated.

Division of labor per the architecture ruling (memory:
agentic-mind-deterministic-body): the MODEL decides which tool answers the
question — policy. This module EXECUTES the call with timeouts and error
containment — mechanism. Neither side does the other's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

ToolRunner = Callable[[dict[str, Any]], Awaitable[str]]
ToolIntent = Callable[[str], dict[str, Any] | None]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: ToolRunner
    # A narrow, deterministic route for an utterance whose intent is clearer
    # than a small local model's tool-call reliability.  The resolver returns
    # None for every utterance it does not explicitly own.
    explicit_intent: ToolIntent | None = None
    # Known word or phrase boundaries that make the tool-decision generation
    # relevant. ``None`` retains the conservative behavior for a future or
    # third-party tool: ask the model for every utterance rather than silently
    # making that tool unreachable. An empty tuple means explicit_intent owns
    # the tool's entire spoken surface.
    decision_hints: tuple[str, ...] | None = None
    # A tool that has to leave the Mac (an article fetch) may need longer
    # than the belt's default ceiling. None keeps the belt's own deadline.
    timeout_s: float | None = None


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


class ToolBelt:
    """The registry the reply loop consults and executes against."""

    def __init__(self, tools: list[Tool] | None = None,
                 timeout_s: float = 10.0) -> None:
        self._tools = {t.name: t for t in (tools or [])}
        self._timeout_s = timeout_s

    def __bool__(self) -> bool:
        return bool(self._tools)

    def register(self, tool: Tool) -> None:
        """Add or replace a provider that becomes available after engine boot."""
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style schemas, the shape Llama chat templates consume."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def tool_definitions(self) -> list[dict[str, Any]]:
        """The belt as name/description/schema, for the MCP route to publish.

        Same tools, same runners; only the envelope differs from ``schemas``.
        A missing description falls back to the name, and a schema without a
        type is an object — a client rejects either omission outright.
        """
        definitions: list[dict[str, Any]] = []
        for t in self._tools.values():
            schema = dict(t.parameters) if isinstance(t.parameters, dict) else {}
            schema.setdefault("type", "object")
            definitions.append({
                "name": t.name,
                "description": t.description or t.name,
                "input_schema": schema,
            })
        return definitions

    def explicit_call(self, text: str) -> ToolCall | None:
        """Return a narrowly owned direct tool call, if any.

        This is deliberately opt-in per tool.  It is not a keyword router for
        the whole belt: ordinary questions still go through the model's tool
        decision pass.  An intent resolver never receives or returns private
        context beyond the spoken utterance and its public tool arguments.
        """
        for tool in self._tools.values():
            resolver = tool.explicit_intent
            if resolver is None:
                continue
            try:
                arguments = resolver(text)
            except Exception:
                # A resolver is a convenience, never a reason to lose the
                # normal model-directed tool path.  Do not log the utterance.
                log.warning("tool intent resolver failed: %s", tool.name)
                continue
            if arguments is not None:
                return ToolCall(name=tool.name, arguments=arguments)
        return None

    def requires_model_decision(self, text: str) -> bool:
        """Whether this utterance merits the extra local-model tool pass.

        Production tools declare narrow word/phrase hints, so an ordinary
        question pays for one answer generation instead of an unproductive
        decision generation plus its answer. Unknown future tools retain the
        former always-decide behavior until they explicitly define safe hints.
        """
        normalized = " ".join(re.findall(r"[a-z0-9]+", text.casefold()))
        padded = f" {normalized} "
        for tool in self._tools.values():
            hints = tool.decision_hints
            if hints is None:
                return True
            for hint in hints:
                normalized_hint = " ".join(re.findall(r"[a-z0-9]+", hint.casefold()))
                if normalized_hint and f" {normalized_hint} " in padded:
                    return True
        return False

    @staticmethod
    def parse_call(text: str) -> ToolCall | None:
        """A tool call if the model emitted one, else None (plain answer).

        Llama 3.2 emits {"name": ..., "parameters": {...}} — sometimes with
        prose around it, so scan for the outermost JSON object rather than
        requiring a clean parse of the whole string.
        """
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        payload = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
                    break
        else:
            return None
        if not isinstance(payload, dict):
            return None
        name = payload.get("name")
        args = payload.get("parameters", payload.get("arguments", {}))
        if not isinstance(name, str) or not isinstance(args, dict):
            return None
        return ToolCall(name=name, arguments=args)

    async def execute(self, call: ToolCall) -> str:
        """Run a call; ALWAYS returns a string the model can reason over.

        A tool failure is information, not an exception — the model should
        get "the weather source timed out" and say so, not crash the reply.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            return f"error: no tool named {call.name!r}"
        deadline = tool.timeout_s if tool.timeout_s is not None else self._timeout_s
        try:
            result = await asyncio.wait_for(tool.run(call.arguments), deadline)
            # Tool arguments may be an exact place category, a message search,
            # or another piece of wearer speech.  A tool name and bounded
            # result length are sufficient operational evidence.
            log.info("tool %s -> %d chars", call.name, len(result))
            return result
        except asyncio.TimeoutError:
            return f"error: {call.name} timed out after {deadline:.0f}s"
        except Exception as exc:
            # Provider error strings can include request echoing.  Keep the
            # diagnostic content-free while preserving a stable result for
            # the answer pass to turn into a truthful recovery message.
            log.warning("tool %s failed (%s)", call.name, type(exc).__name__)
            return f"error: {call.name} failed"
