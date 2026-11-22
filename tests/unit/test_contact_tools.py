"""The assistant's read-only Contacts lookup over the Mac's native bridge."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from joat.agents.contact_tools import contacts_lookup_tool
from joat.native.bridge import NativeBridgeError, NativeBridgeUnavailable


class FakeBridge:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> object:
        self.calls.append((method, params))
        if isinstance(self.response, Exception):
            raise self.response
        return SimpleNamespace(result=self.response)


ADA = {
    "name": "Ada Lovelace", "nickname": "", "organization": "Analytical Engines", "job_title": "Engineer",
    "phones": [{"label": "mobile", "value": "+1 555 0100"}], "emails": [{"label": "work", "value": "ada@example.com"}],
    "birthday": "12-10",
}


async def test_finds_a_contact_and_marks_it_as_data() -> None:
    bridge = FakeBridge([ADA])
    text = await contacts_lookup_tool(bridge).run({"query": " Ada "})
    assert bridge.calls == [("contacts.search", {"query": "Ada", "limit": 10})]
    assert "not instructions" in text
    assert "Ada Lovelace — Engineer · Analytical Engines" in text
    assert "phone: +1 555 0100 (mobile)" in text and "email: ada@example.com (work)" in text
    assert "birthday: 12-10" in text


async def test_says_when_nothing_matches() -> None:
    assert "No contact matches 'Zed'" in await contacts_lookup_tool(FakeBridge([])).run({"query": "Zed"})


async def test_access_and_bridge_failures_are_plain_sentences() -> None:
    denied = NativeBridgeError("contacts_not_authorized", "x", retryable=False, commit_phase="read_only")
    assert "Grant Access" in await contacts_lookup_tool(FakeBridge(denied)).run({"query": "Ada"})
    old = NativeBridgeError("unknown_method", "x", retryable=False, commit_phase="read_only")
    assert "too old" in await contacts_lookup_tool(FakeBridge(old)).run({"query": "Ada"})
    down = NativeBridgeUnavailable("down")
    assert "could not be read" in await contacts_lookup_tool(FakeBridge(down)).run({"query": "Ada"})
    assert "Grant Access" in await contacts_lookup_tool(None).run({"query": "Ada"})


async def test_rejects_an_empty_or_huge_query_without_calling() -> None:
    bridge = FakeBridge([ADA])
    tool = contacts_lookup_tool(bridge)
    assert "1 to 100" in await tool.run({"query": "  "})
    assert "1 to 100" in await tool.run({"query": "x" * 101})
    assert bridge.calls == []
