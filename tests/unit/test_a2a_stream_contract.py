"""Independent A2A v1 event oracle based on Hermes protocol.py:184-204.

The transport boundary has already validated/removed the JSON-RPC envelope.
These events deliberately do not use JOAT's parser to construct expected data.
"""
import pytest

from joat.agents.a2a_terminal import A2ATerminalAgent
from joat.core.a2a import A2AError, A2ARPCError


class ContractTransport:
    def __init__(self, events):
        self.events = events
        self.calls = []

    async def post_jsonrpc(self, endpoint, payload, token):
        self.calls.append(payload["method"])
        assert payload["method"] == "tasks/get"
        raise A2ARPCError(-32001)

    async def post_jsonrpc_events(self, endpoint, payload, token):
        self.calls.append(payload["method"])
        assert payload["method"] == "message/stream"
        for event in self.events:
            yield event


async def token():
    return "fixture-only"


def status(state, task="owned"):
    return {"statusUpdate": {"taskId": task, "contextId": "conversation",
                             "status": {"state": state}}}


def artifact(text, append=False, task="owned"):
    return {"artifactUpdate": {"taskId": task, "contextId": "conversation", "append": append,
                               "artifact": {"artifactId": "reply", "parts": [{"text": text}]}}}


@pytest.mark.parametrize("initial_task", [True, False])
async def test_v1_nested_events_assemble_exact_text_and_publish_partial_answer(initial_task):
    events = ([{"task": {"id": "owned", "contextId": "conversation",
                         "status": {"state": "TASK_STATE_SUBMITTED"}}}] if initial_task else [])
    events += [status("TASK_STATE_WORKING"), artifact("Hello"), artifact(" world", True),
               status("TASK_STATE_COMPLETED")]
    transport = ContractTransport(events)
    agent = A2ATerminalAgent("https://fixture.invalid/", token, "Renamed",
                             transport=transport, streaming=True, provider="hermes")
    await agent.initialize()
    updates = []
    async def updated(result):
        updates.append(result)
    reply = "".join([part async for part in agent.stream_task_captured(
        "Say hello", context_generation=agent.capture_context_generation, on_update=updated)])
    assert reply == "Hello world"
    assert any(update.state == "working" and update.text == "Hello world" for update in updates)
    assert transport.calls == ["tasks/get", "message/stream"]


@pytest.mark.parametrize("bad_event", [artifact("wrong", task="foreign"), status("TASK_STATE_COMPLETED", "foreign")])
async def test_nested_event_cannot_switch_owned_task_or_trigger_resend(bad_event):
    transport = ContractTransport([status("TASK_STATE_WORKING"), bad_event])
    agent = A2ATerminalAgent("https://fixture.invalid/", token, "Hermes",
                             transport=transport, streaming=True, provider="hermes")
    with pytest.raises(A2AError):
        _ = [part async for part in agent.stream("Keep ownership")]
    assert agent.needs_recovery
    assert transport.calls == ["tasks/get", "message/stream"]


async def test_interrupted_stream_subscribes_to_owned_task_without_resending():
    class Resumable(ContractTransport):
        async def post_jsonrpc_events(self, endpoint, payload, token):
            self.calls.append(payload['method'])
            if payload['method'] == 'message/stream':
                yield status('TASK_STATE_WORKING')
                yield artifact('Hello')
            else:
                assert payload['method'] == 'tasks/subscribe'
                assert payload['params'] == {'id': 'owned'}
                yield artifact(' world', True)
                yield status('TASK_STATE_COMPLETED')
    transport = Resumable([])
    agent = A2ATerminalAgent('https://fixture.invalid/', token, 'Hermes', transport=transport, streaming=True)
    assert ''.join([part async for part in agent.stream('Hello')]) == 'Hello world'
    assert transport.calls == ['tasks/get', 'message/stream', 'tasks/subscribe']


async def test_authorization_blocks_input_until_owned_task_is_resolved():
    class Authorization(ContractTransport):
        async def post_jsonrpc(self, endpoint, payload, token):
            self.calls.append(payload['method'])
            if payload['params']['id'] != 'owned':
                raise A2ARPCError(-32001)
            return {'id': 'owned', 'contextId': 'conversation', 'status': {'state': 'TASK_STATE_AUTH_REQUIRED'}}
    transport = Authorization([status('TASK_STATE_AUTH_REQUIRED')])
    agent = A2ATerminalAgent('https://fixture.invalid/', token, 'Renamed', transport=transport, streaming=True, provider='hermes')
    with pytest.raises(A2AError):
        _ = [part async for part in agent.stream('Do authorized work')]
    assert agent.needs_recovery and agent.capture_context_generation is None
    guidance = await agent.recover()
    assert 'Hermes' in guidance and 'authorization' in guidance
    assert agent.needs_recovery and agent.capture_context_generation is None
    assert transport.calls.count('message/stream') == 1


async def test_large_valid_reply_export_restores_without_disabling_conversation():
    transport = ContractTransport([status('TASK_STATE_WORKING'), artifact('A' * 20000), status('TASK_STATE_COMPLETED')])
    agent = A2ATerminalAgent('https://fixture.invalid/', token, 'Hermes', transport=transport, streaming=True)
    _ = [part async for part in agent.stream('Long answer')]
    restored = A2ATerminalAgent('https://fixture.invalid/', token, 'Renamed', transport=transport, streaming=True)
    restored.restore_state(agent.export_state())
    assert await restored.history() == [('user', 'Long answer'), ('agent', 'A' * 20000)]
    assert not restored.needs_recovery
