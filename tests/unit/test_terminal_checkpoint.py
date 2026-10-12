from __future__ import annotations

import json
from pathlib import Path

import pytest

from joat.agents.a2a_terminal import A2ATerminalAgent
from joat.core.terminal_conversation import TerminalConversation
from joat.persistence.store import DurableStore
from joat.persistence.terminal import TerminalCheckpointError, TerminalCheckpointStore
from joat.services.agent_terminal import AgentTerminalRoster

ENDPOINT = 'https://fixture.invalid/a2a'

async def token():
    return 'test-only'


def test_checkpoint_roundtrip_preserves_whole_content_and_next_identity(tmp_path):
    original = TerminalConversation()
    original.append('user', '  Keep indentation\n\nAnd paragraphs')
    original.append('assistant', 'Long reply ' * 3000)
    with DurableStore(tmp_path / 'state.sqlite') as db:
        repo = TerminalCheckpointStore(db)
        repo.save('agent', ENDPOINT, original, {'history': []})
        loaded = repo.load('agent', ENDPOINT)
        restored = TerminalConversation.restore(loaded['conversation'])
        assert restored.snapshot() == original.snapshot()
        assert restored.append('user', 'continue') == original.id + ':3'
        assert repo.load('different', ENDPOINT) is None
        assert repo.load('agent', ENDPOINT + '/other') is None


def test_checkpoint_rejects_credentials_and_rolls_back_prior_snapshot(tmp_path):
    with DurableStore(tmp_path / 'state.sqlite') as db:
        repo = TerminalCheckpointStore(db)
        conversation = TerminalConversation()
        repo.save('agent', ENDPOINT, conversation, {'history': []})
        before = repo.load('agent', ENDPOINT)
        with pytest.raises(TerminalCheckpointError):
            repo.save('agent', ENDPOINT, conversation, {'bearer_token': 'must not persist'})
        assert repo.load('agent', ENDPOINT) == before
        assert b'must not persist' not in (tmp_path / 'state.sqlite').read_bytes()


def test_provider_change_or_corruption_blocks_only_that_lane(tmp_path):
    with DurableStore(tmp_path / 'state.sqlite') as db:
        repo = TerminalCheckpointStore(db)
        first = A2ATerminalAgent(ENDPOINT, token, 'Old name')
        roster = AgentTerminalRoster([('old', first)], visible_lanes=('old',))
        assert roster.bind_persistence('old', repo, 'stable', ENDPOINT, provider='hermes')
        renamed = A2ATerminalAgent(ENDPOINT, token, 'New name')
        r = AgentTerminalRoster([('new', renamed)], visible_lanes=('new',))
        assert r.bind_persistence('new', repo, 'stable', ENDPOINT, provider='hermes')
        assert r.frame().conversation_id == roster.frame().conversation_id
        changed = AgentTerminalRoster([('agent', A2ATerminalAgent(ENDPOINT, token, 'Agent'))])
        assert not changed.bind_persistence('agent', repo, 'stable', ENDPOINT, provider='openclaw')
        assert changed.lane('agent').persistence_error
        assert changed._capture_target() is None
        db.execute('UPDATE terminal_checkpoints SET snapshot=?', (b'{}',))
        corrupt = AgentTerminalRoster([('bad', A2ATerminalAgent(ENDPOINT, token, 'Bad')),
                                      ('good', A2ATerminalAgent(ENDPOINT + '/good', token, 'Good'))])
        assert not corrupt.bind_persistence('bad', repo, 'stable', ENDPOINT, provider='hermes')
        assert corrupt.bind_persistence('good', repo, 'other', ENDPOINT + '/good')


def test_migration_12_preserves_v11_ledger_and_timer_data(tmp_path, monkeypatch):
    from joat.persistence import store as module
    current = module._MIGRATIONS
    path = tmp_path / 'upgrade.sqlite'
    with monkeypatch.context() as baseline:
        baseline.setattr(module, '_MIGRATIONS', current[:11])
        with DurableStore(path) as db:
            ledger = db.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
            db.execute("INSERT INTO pomodoro_checkpoint VALUES(1,'focus',0,60000,60000,0,NULL,42)")
            timer = db.execute('SELECT * FROM pomodoro_checkpoint').fetchone()
    with DurableStore(path) as db:
        # 12 terminal_checkpoints; 13 pomodoro_lengths; 14 saved_places;
        # 15 live QuickNote transcription (FEAT-002)
        assert db.schema_version == 16
        assert db.execute('SELECT * FROM schema_migrations WHERE version<=11 ORDER BY version').fetchall() == ledger
        assert db.execute('SELECT * FROM pomodoro_checkpoint').fetchone() == timer
        assert db.execute('SELECT count(*) FROM terminal_checkpoints').fetchone() == (0,)
        assert db.check_integrity() == ('ok', ())


async def test_storage_failure_before_dispatch_never_submits_work(tmp_path, monkeypatch):
    from joat.core.a2a import A2ARPCError
    calls = []
    class Transport:
        async def post_jsonrpc(self, endpoint, payload, token):
            calls.append(payload['method'])
            if payload['method'] == 'tasks/get':
                raise A2ARPCError(-32001)
            raise AssertionError('Storage failure must prohibit message/send')
    with DurableStore(tmp_path / 'state.sqlite') as db:
        repo = TerminalCheckpointStore(db)
        agent = A2ATerminalAgent(ENDPOINT, token, 'Agent', transport=Transport())
        roster = AgentTerminalRoster([('agent', agent)], visible_lanes=('agent',))
        assert roster.bind_persistence('agent', repo, 'stable', ENDPOINT)
        await agent.initialize()
        def fail(*args, **kwargs):
            raise TerminalCheckpointError('simulated disk full')
        monkeypatch.setattr(repo, 'save', fail)
        await roster.send('A consequential request')
        await roster.lane('agent').task
        assert calls == ['tasks/get']
        await roster.detach_for_shutdown()  # Failure cannot stop remaining engine cleanup.


async def test_shutdown_detaches_work_without_cancel_even_if_checkpoint_fails(tmp_path, monkeypatch):
    import asyncio
    from joat.core.a2a import A2ARPCError
    started = asyncio.Event()
    held = asyncio.Event()
    calls = []
    class Transport:
        async def post_jsonrpc(self, endpoint, payload, token):
            method = payload['method']
            calls.append(method)
            if method == 'tasks/get' and payload['params']['id'] != 'owned':
                raise A2ARPCError(-32001)
            if method == 'tasks/get':
                started.set()
                await held.wait()
            assert method != 'tasks/cancel', 'Engine restart is not wearer Stop'
            return {'id':'owned','contextId':'context','status':{'state':'TASK_STATE_WORKING'},'artifacts':[]}
    with DurableStore(tmp_path / 'state.sqlite') as db:
        repo = TerminalCheckpointStore(db)
        agent = A2ATerminalAgent(ENDPOINT, token, 'Agent', transport=Transport(), poll_interval_s=0)
        r = AgentTerminalRoster([('agent', agent)], visible_lanes=('agent',))
        assert r.bind_persistence('agent', repo, 'stable', ENDPOINT)
        await agent.initialize()
        await r.send('Long running request')
        await asyncio.wait_for(started.wait(), 2)
        def fail(*args, **kwargs):
            raise TerminalCheckpointError('simulated shutdown disk failure')
        monkeypatch.setattr(repo, 'save', fail)
        await asyncio.wait_for(r.detach_for_shutdown(), 2)
        assert r.lane('agent').task.done()
        assert 'tasks/cancel' not in calls
        assert any('Observation paused' in m.text for m in r.frame().messages)
        assert not any('Remote stop unconfirmed' in m.text for m in r.frame().messages)


async def test_stream_snapshots_and_final_reply_keep_one_assistant_identity():
    from joat.core.a2a import A2ARequestResult
    class Streaming:
        streaming_enabled = True
        capture_context_generation = 1
        async def stream(self, prompt):
            yield 'unused'
        async def stream_captured(self, prompt, *, context_generation):
            yield 'unused'
        async def stream_task_captured(self, prompt, *, context_generation, on_update):
            for text in ('Hello', 'Hello world', 'Hello world!'):
                await on_update(A2ARequestResult('task','context','working',text,'Composing'))
            await on_update(A2ARequestResult('task','context','completed','Hello world!',''))
            yield 'Hello world!'
    frames = []
    async def paint(frame): frames.append(frame)
    roster = AgentTerminalRoster([('agent', Streaming())], visible_lanes=('agent',), paint=paint)
    await roster.send('Say hello')
    await roster.lane('agent').task
    answers = [m for frame in frames for m in frame.messages if m.role == 'assistant']
    assert {'Hello','Hello world','Hello world!'} <= {m.text for m in answers}
    assert len({m.id for m in answers}) == 1
    assert [m.text for m in roster.frame().messages if m.role == 'assistant'] == ['Hello world!']


async def test_repeated_recovery_replaces_partial_answer_without_duplicate_messages():
    from joat.core.a2a import A2ARequestResult
    class Recoverable:
        last_outcome = A2ARequestResult('owned', 'context', 'completed', 'Complete answer')
        async def stream(self, prompt):
            yield 'unused'
        async def recover(self):
            return 'Complete answer'
    roster = AgentTerminalRoster([('agent', Recoverable())], visible_lanes=('agent',))
    conversation = roster.lane('agent').conversation
    conversation.append('user', 'Explain')
    partial_id = conversation.append('assistant', 'Partial answer')
    conversation.append('status', 'Working: still working')
    conversation.append('status', 'Check again or cancel')
    await roster.recover_task()
    await roster.recover_task()
    answers = [m for m in roster.frame().messages if m.role == 'assistant']
    assert [(m.id, m.text) for m in answers] == [(partial_id, 'Complete answer')]
    assert [m.text for m in roster.frame().messages if m.role == 'status'] == ['Completed']


async def test_new_conversation_is_committed_before_success_without_a_paint(tmp_path):
    async def token(): return 'fixture-only'
    with DurableStore(tmp_path / 'state.sqlite') as db:
        repository = TerminalCheckpointStore(db)
        agent = A2ATerminalAgent(ENDPOINT, token, 'Agent')
        roster = AgentTerminalRoster([('agent', agent)], visible_lanes=('agent',))
        assert roster.bind_persistence('agent', repository, 'stable', ENDPOINT)
        old_identity = roster.frame().conversation_id
        assert 'new conversation' in await roster.new_conversation()
        saved = repository.load('stable', ENDPOINT)
        assert saved['conversation']['id'] == roster.frame().conversation_id != old_identity
        assert saved['adapter']['context'] is None


async def test_shutdown_drains_pending_recovery_before_store_closes(tmp_path):
    import asyncio
    from joat.core.a2a import A2ARequestResult
    started = asyncio.Event()
    release = asyncio.Event()
    class Transport:
        async def post_jsonrpc(self, endpoint, payload, token):
            started.set()
            await release.wait()
            return {'id': 'owned', 'contextId': 'context',
                    'status': {'state': 'TASK_STATE_COMPLETED'}, 'artifacts': []}
    db = DurableStore(tmp_path / 'state.sqlite')
    agent = A2ATerminalAgent(ENDPOINT, token, 'Agent', transport=Transport())
    agent._task, agent._context, agent._uncertain = 'owned', 'context', True
    agent.last_outcome = A2ARequestResult('owned', 'context', 'working', '')
    roster = AgentTerminalRoster([('agent', agent)], visible_lanes=('agent',))
    assert roster.bind_persistence('agent', TerminalCheckpointStore(db), 'stable', ENDPOINT)
    visit = asyncio.create_task(roster.prepare_lane('agent', is_current=lambda: True))
    await asyncio.wait_for(started.wait(), 2)
    await roster.detach_for_shutdown()
    preparation = roster.lane('agent').preparation
    assert preparation.abandoned and preparation.task.done()
    await agent.close()
    db.close()
    release.set()
    await asyncio.gather(visit, return_exceptions=True)
    assert agent.capture_context_generation is None
    assert not await roster.prepare_lane('agent', is_current=lambda: True)
