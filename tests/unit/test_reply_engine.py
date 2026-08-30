"""Focused lifecycle tests for the resident MLX reply engine."""

from __future__ import annotations

from typing import Any

import pytest

from joat.agents.reply_engine import MlxReplyEngine


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
