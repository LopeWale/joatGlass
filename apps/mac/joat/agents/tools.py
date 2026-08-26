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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

ToolRunner = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: ToolRunner


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
        try:
            result = await asyncio.wait_for(
                tool.run(call.arguments), self._timeout_s)
            log.info("tool %s(%s) -> %d chars", call.name,
                     json.dumps(call.arguments)[:80], len(result))
            return result
        except asyncio.TimeoutError:
            return f"error: {call.name} timed out after {self._timeout_s:.0f}s"
        except Exception as exc:
            log.warning("tool %s failed: %s", call.name, exc)
            return f"error: {call.name} failed: {exc}"
