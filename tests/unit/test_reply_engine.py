"""Focused lifecycle tests for the resident MLX reply engine."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from joat.agents.reply_engine import MlxReplyEngine
from joat.agents.tools import Tool, ToolBelt


class FakeMlxReplyEngine(MlxReplyEngine):
    def __init__(self, *, fail_load: bool = False, on_loaded: Any = None) -> None:
        super().__init__(model_id="test-model", on_loaded=on_loaded)
        self.fail_load = fail_load
        self.load_calls = 0

    def _load(self) -> tuple[Any, Any]:
        self.load_calls += 1
        if self.fail_load:
            raise RuntimeError("model unavailable")
        return object(), object()

    def _generate(self, text: str, tool_note: str | None = None) -> str:
        return f"ready:{text}"


@pytest.mark.asyncio
async def test_warm_loads_once_before_the_first_reply() -> None:
    loaded: list[bool] = []
    engine = FakeMlxReplyEngine(on_loaded=lambda: loaded.append(True))
    try:
        assert await engine.warm() is True
        assert await engine.reply("hello") == "ready:hello"
        assert engine.load_calls == 1
        assert loaded == [True]
    finally:
        engine._mlx_thread.shutdown(wait=True)


@pytest.mark.asyncio
async def test_warm_failure_is_contained_and_can_be_retried() -> None:
    engine = FakeMlxReplyEngine(fail_load=True)
    try:
        assert await engine.warm() is False
        engine.fail_load = False
        assert await engine.reply("retry") == "ready:retry"
        assert engine.load_calls == 2
    finally:
        engine._mlx_thread.shutdown(wait=True)


@pytest.mark.asyncio
async def test_explicit_nearby_intent_bypasses_the_unreliable_model_decision_pass() -> None:
    called: list[dict[str, Any]] = []

    async def nearby(args: dict[str, Any]) -> str:
        called.append(args)
        return "Nearby places: Lakeview Cafe"

    engine = MlxReplyEngine(
        model_id="test-model",
        tools=ToolBelt([
            Tool(
                "find_nearby_places", "", {"type": "object"}, nearby,
                explicit_intent=lambda text: {"query": "coffee"} if text == "coffee near me" else None,
            ),
        ]),
    )
    engine._decide = lambda _text: (_ for _ in ()).throw(AssertionError("model should not decide"))
    try:
        assert await engine._maybe_use_tools("coffee near me") == (
            "You looked this up with find_nearby_places: Nearby places: Lakeview Cafe"
        )
        assert called == [{"query": "coffee"}]
    finally:
        engine._mlx_thread.shutdown(wait=True)


@pytest.mark.asyncio
async def test_ordinary_question_skips_known_irrelevant_tool_decision() -> None:
    async def unused_tool(_args: dict[str, Any]) -> str:
        return "unused"

    engine = MlxReplyEngine(
        model_id="test-model",
        tools=ToolBelt([
            Tool(
                "get_weather", "", {"type": "object"}, unused_tool,
                decision_hints=("weather", "rain", "temperature"),
            ),
        ]),
    )
    engine._decide = lambda _text: (_ for _ in ()).throw(
        AssertionError("ordinary question must not pay a tool-decision generation")
    )
    try:
        assert await engine._maybe_use_tools("tell me a short joke") is None
    finally:
        engine._mlx_thread.shutdown(wait=True)


@pytest.mark.asyncio
async def test_tool_relevant_question_keeps_model_decision_when_not_explicit() -> None:
    async def unused_tool(_args: dict[str, Any]) -> str:
        return "unused"

    decided: list[str] = []
    engine = MlxReplyEngine(
        model_id="test-model",
        tools=ToolBelt([
            Tool(
                "get_weather", "", {"type": "object"}, unused_tool,
                decision_hints=("weather", "rain", "temperature"),
            ),
        ]),
    )
    engine._decide = lambda text: decided.append(text) or "plain answer"
    try:
        assert await engine._maybe_use_tools("weather in Paris") is None
        assert decided == ["weather in Paris"]
    finally:
        engine._mlx_thread.shutdown(wait=True)


@pytest.mark.asyncio
async def test_stream_emits_a_small_first_chunk_then_returns_to_steady_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mlx_lm = ModuleType("mlx_lm")

    def stream_generate(*_args: Any, **_kwargs: Any) -> Any:
        for text in ("abcdefgh" + "i" * 16 + "j" * 16, "tail"):
            yield SimpleNamespace(text=text)

    mlx_lm.stream_generate = stream_generate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_lm", mlx_lm)

    engine = MlxReplyEngine(model_id="test-model", chunk_chars=32)
    engine._model = object()
    engine._tokenizer = SimpleNamespace(
        apply_chat_template=lambda *_args, **_kwargs: "prompt",
    )
    try:
        chunks = [chunk async for chunk in engine.stream("hello")]
        assert chunks == ["abcdefgh", "i" * 16 + "j" * 16, "tail"]
    finally:
        engine._mlx_thread.shutdown(wait=True)


@pytest.mark.asyncio
async def test_first_chunk_threshold_is_configurable_for_latency_trials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mlx_lm = ModuleType("mlx_lm")

    def stream_generate(*_args: Any, **_kwargs: Any) -> Any:
        yield SimpleNamespace(text="a" * 48)

    mlx_lm.stream_generate = stream_generate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_lm", mlx_lm)

    engine = MlxReplyEngine(
        model_id="test-model", chunk_chars=32, first_chunk_chars=16
    )
    engine._model = object()
    engine._tokenizer = SimpleNamespace(
        apply_chat_template=lambda *_args, **_kwargs: "prompt",
    )
    try:
        chunks = [chunk async for chunk in engine.stream("hello")]
        assert chunks == ["a" * 16, "a" * 32]
    finally:
        engine._mlx_thread.shutdown(wait=True)


@pytest.mark.asyncio
async def test_stream_splits_an_oversized_single_model_delta_after_the_first_paint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mlx_lm = ModuleType("mlx_lm")

    def stream_generate(*_args: Any, **_kwargs: Any) -> Any:
        yield SimpleNamespace(text="a" * 88)

    mlx_lm.stream_generate = stream_generate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_lm", mlx_lm)

    engine = MlxReplyEngine(model_id="test-model", chunk_chars=32)
    engine._model = object()
    engine._tokenizer = SimpleNamespace(
        apply_chat_template=lambda *_args, **_kwargs: "prompt",
    )
    try:
        chunks = [chunk async for chunk in engine.stream("hello")]
        assert chunks == ["a" * 8, "a" * 32, "a" * 32, "a" * 16]
    finally:
        engine._mlx_thread.shutdown(wait=True)
