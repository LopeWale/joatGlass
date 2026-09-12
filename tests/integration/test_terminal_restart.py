"""Real process boundaries; only the provider transport is an in-memory peer."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

WORKER = r'''
import asyncio, json, os, sys
from pathlib import Path
from joat.agents.a2a_terminal import A2ATerminalAgent
from joat.core.a2a import A2ARPCError
from joat.persistence.store import DurableStore
from joat.persistence.terminal import TerminalCheckpointStore
from joat.services.agent_terminal import AgentTerminalRoster

root, mode, stage = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
calls = []
class Transport:
    async def post_jsonrpc(self, endpoint, payload, token):
        method = payload['method']
        calls.append(method)
        with (root/'network-calls.jsonl').open('a') as f:
            f.write(json.dumps({'stage':stage,'method':method,'params':payload['params']})+'\n')
        if method == 'tasks/get' and payload['params']['id'] != 'owned-task':
            raise A2ARPCError(-32001)
        if mode == 'after_server_accept' and stage == 'write' and method == 'message/send':
            os._exit(0)
        state = 'TASK_STATE_WORKING' if mode == 'send_accepted' and stage == 'write' else 'TASK_STATE_COMPLETED'
        if mode == 'input_required' and stage == 'write':
            return {'id':'owned-task','contextId':'owned-context','status':{'state':'TASK_STATE_INPUT_REQUIRED','message':{'parts':[{'kind':'text','text':'Which city should I use?'}]}},'artifacts':[]}
        return {'id':'owned-task','contextId':'owned-context','status':{'state':state},
                'artifacts':[{'parts':[{'kind':'text','text':'Durable amber answer'}]}]}
async def token(): return 'simulator-only'
async def main():
    store = DurableStore(root/'terminal.sqlite')
    agent = A2ATerminalAgent('https://fixture.invalid/a2a',token,'Agent',transport=Transport())
    roster = AgentTerminalRoster([('agent',agent)],visible_lanes=('agent',))
    assert roster.bind_persistence('agent',TerminalCheckpointStore(store),'stable','https://fixture.invalid/a2a')
    if stage == 'write':
        (root/'identity').write_text(roster.frame().conversation_id)
        checkpoint = agent._checkpoint
        async def crash_after_commit(state):
            await checkpoint(state)
            if state.get('checkpoint_phase') == mode:
                os._exit(0)
        agent.set_checkpoint(crash_after_commit)
        await agent.initialize()
        await roster.send('Remember amber')
        await roster.lane('agent').task
        await roster.detach_for_shutdown()
        store.close()
        return
    assert calls == [], 'Restoring a checkpoint must not replay network work'
    assert roster.frame().conversation_id == (root/'identity').read_text()
    before = [m.text for m in roster.frame().messages]
    if mode in {'pre_send','after_server_accept'}:
        assert agent.needs_recovery
        assert agent.export_state()['task'] is None
        assert 'unknown' in (await roster.recover_task()).lower()
        assert calls == []
    elif mode == 'send_accepted':
        assert agent.needs_recovery
        assert agent.export_state()['task'] == 'owned-task'
        assert await roster.recover_task() == 'Durable amber answer'
        assert calls == ['tasks/get']
    elif mode == 'input_required':
        assert 'Which city should I use?' in before
        await agent.initialize()
        await roster.send('Chicago')
        await roster.lane('agent').task
    else:
        assert 'Durable amber answer' in before, before
        assert await agent.history() == [('user','Remember amber'),('agent','Durable amber answer')]
        await agent.initialize()
        await roster.send('Continue with that answer')
        await roster.lane('agent').task
    print(json.dumps({'before':before,'calls':calls,'identity':roster.frame().conversation_id}))
    await roster.detach_for_shutdown()
    store.close()
asyncio.run(main())
'''

@pytest.mark.parametrize('mode', ['complete', 'pre_send', 'after_server_accept', 'send_accepted', 'result', 'input_required'])
def test_process_restart_preserves_owned_state_without_automatic_replay(tmp_path: Path, mode: str):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'apps/mac')}
    for stage in ('write', 'read'):
        result = subprocess.run([sys.executable, '-c', WORKER, str(tmp_path), mode, stage],
                                env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
    result_data = json.loads(result.stdout)
    calls = [json.loads(line) for line in (tmp_path/'network-calls.jsonl').read_text().splitlines()]
    sends = [row for row in calls if row['method'] == 'message/send']
    assert len(sends) == (0 if mode == 'pre_send' else 2 if mode in {'complete','result','input_required'} else 1)
    if len(sends) == 2:
        followup = sends[-1]['params']['message']
        assert followup['contextId'] == 'owned-context'
        if mode == 'input_required':
            assert followup['taskId'] == 'owned-task'
            assert 'Which city should I use?' in followup['parts'][0]['text']
        else:
            assert 'Durable amber answer' in followup['parts'][0]['text']
    assert 'tasks/cancel' not in [row['method'] for row in calls]
