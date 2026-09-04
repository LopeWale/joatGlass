"""ToolBelt: the model decides, the runtime executes.

The ruling this implements: an assistant that stops at "I don't know" is as
fired as one that invents the answer — it goes and figures out what is
needed. The LLM picks the tool (the pathfinding deterministic code cannot
do); this layer runs it with timeouts and error containment.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from joat.agents.tools import Tool, ToolBelt, ToolCall


def make_tool(name: str = "probe", result: str = "42") -> Tool:
    async def run(args: dict[str, Any]) -> str:
        return result

    return Tool(name=name, description="", parameters={"type": "object"}, run=run)


class TestParseCall:
    def test_clean_call_parses(self) -> None:
        # Verbatim what the live 3B emitted in the probe.
        call = ToolBelt.parse_call(
            '{"name": "get_weather", "parameters": {"location": "Norman, Oklahoma"}}')
        assert call == ToolCall("get_weather", {"location": "Norman, Oklahoma"})

    def test_call_wrapped_in_prose_still_parses(self) -> None:
        call = ToolBelt.parse_call('Sure! {"name": "t", "parameters": {"x": 1}} done')
        assert call == ToolCall("t", {"x": 1})

    def test_plain_text_is_not_a_call(self) -> None:
        assert ToolBelt.parse_call("The time is 5:20 PM.") is None

    def test_json_that_is_not_a_call_is_not_a_call(self) -> None:
        assert ToolBelt.parse_call('{"weather": "sunny"}') is None

    def test_arguments_key_variant_parses(self) -> None:
        call = ToolBelt.parse_call('{"name": "t", "arguments": {"x": 1}}')
        assert call == ToolCall("t", {"x": 1})

    def test_malformed_json_is_not_a_call(self) -> None:
        assert ToolBelt.parse_call('{"name": "t", "parameters": {') is None


class TestExecute:
    async def test_runs_the_named_tool(self) -> None:
        belt = ToolBelt([make_tool(result="got it")])
        assert await belt.execute(ToolCall("probe", {})) == "got it"

    async def test_unknown_tool_is_an_error_string_not_an_exception(self) -> None:
        belt = ToolBelt([make_tool()])
        out = await belt.execute(ToolCall("nope", {}))
        assert out.startswith("error:")

    async def test_a_raising_tool_becomes_content_free_information(
        self, caplog: Any,
    ) -> None:
        async def boom(args: dict[str, Any]) -> str:
            raise RuntimeError("sensor offline: the place I told you about")

        caplog.set_level(logging.WARNING, logger="joat.agents.tools")
        belt = ToolBelt([Tool("bad", "", {"type": "object"}, boom)])
        out = await belt.execute(ToolCall("bad", {}))
        assert out == "error: bad failed"
        assert "the place I told you about" not in caplog.text
        assert "tool bad failed (RuntimeError)" in caplog.text

    async def test_a_hung_tool_times_out_into_information(self) -> None:
        async def hang(args: dict[str, Any]) -> str:
            await asyncio.sleep(60)
            return "never"

        belt = ToolBelt([Tool("slow", "", {"type": "object"}, hang)], timeout_s=0.05)
        out = await belt.execute(ToolCall("slow", {}))
        assert "timed out" in out

    def test_empty_belt_is_falsy(self) -> None:
        assert not ToolBelt()
        assert ToolBelt([make_tool()])

    async def test_register_makes_a_late_bound_provider_available_to_the_same_reply_engine(self) -> None:
        belt = ToolBelt()
        belt.register(make_tool(name="find_nearby_places", result="one nearby café"))

        assert await belt.execute(ToolCall("find_nearby_places", {})) == "one nearby café"

    def test_explicit_intent_is_opt_in_and_returns_a_typed_call(self) -> None:
        belt = ToolBelt([
            make_tool(),
            Tool(
                "nearby", "", {"type": "object"},
                lambda _args: _resolved("nearby"),
                explicit_intent=lambda text: {"query": "coffee"} if text == "coffee near me" else None,
            ),
        ])

        assert belt.explicit_call("coffee near me") == ToolCall("nearby", {"query": "coffee"})
        assert belt.explicit_call("tell me a joke") is None

    def test_known_tool_hints_skip_an_unrelated_model_decision(self) -> None:
        belt = ToolBelt([
            Tool(
                "weather", "", {"type": "object"}, _resolved,
                decision_hints=("weather", "rain"),
            ),
        ])

        assert belt.requires_model_decision("tell me a joke") is False
        assert belt.requires_model_decision("tell me about brains") is False
        assert belt.requires_model_decision("will it rain in Chicago tomorrow") is True

    def test_unknown_future_tools_keep_the_conservative_model_decision_path(self) -> None:
        belt = ToolBelt([make_tool()])

        assert belt.requires_model_decision("tell me a joke") is True

    async def test_execution_log_does_not_retain_tool_arguments(
        self, caplog: Any,
    ) -> None:
        caplog.set_level(logging.INFO, logger="joat.agents.tools")
        private_query = "the place I told you about"
        belt = ToolBelt([make_tool(result="one result")])

        assert await belt.execute(ToolCall("probe", {"query": private_query})) == "one result"
        assert private_query not in caplog.text
        assert "tool probe -> 10 chars" in caplog.text


async def _resolved(value: str) -> str:
    return value


class TestToolDefinitions:
    """What the MCP route publishes to the CLI seats."""

    def test_name_description_and_schema_travel_together(self) -> None:
        async def run(args: dict[str, Any]) -> str:
            return ""

        belt = ToolBelt([
            Tool("get_weather", "Weather now.", {"type": "object", "properties": {}}, run),
            Tool("bare", "", {"properties": {"q": {"type": "string"}}}, run),
        ])
        definitions = belt.tool_definitions()
        assert definitions[0] == {
            "name": "get_weather", "description": "Weather now.",
            "input_schema": {"type": "object", "properties": {}},
        }
        # A description is required by clients, so the name stands in; a
        # schema without a declared type is an object.
        assert definitions[1]["description"] == "bare"
        assert definitions[1]["input_schema"]["type"] == "object"

    def test_an_empty_belt_publishes_nothing(self) -> None:
        assert ToolBelt([]).tool_definitions() == []
