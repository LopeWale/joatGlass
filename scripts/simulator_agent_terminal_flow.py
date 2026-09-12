#!/usr/bin/env python3
"""Isolated official-simulator Agent Terminal journey, with local TLS A2A servers.

Never reads pairing/configuration/credentials or contacts the installed engine.
The only synthetic boundaries are two local A2A services and final STT text.
All menu navigation, prompt acknowledgment, routing, adapter parsing and HUD
rendering use production code. Screenshots and semantic receipts are retained.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import ssl
import socket
import subprocess
import sys
import urllib.request
from aiohttp import web
from PIL import Image
from joat.agents.a2a_terminal import A2AHttpTransport, A2ATerminalAgent
from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.interfaces import Transcript
from joat.core.protocol import AgentTerminalHudView
from joat.services.agent_terminal import AgentTerminalCommands, AgentTerminalRoster
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.message_reply import MessageReplyController
from joat.services.prompted_input import UtteranceProvenance
from joat.services.prompted_speech import PromptedSpeechRuntime
from joat.services.session import SessionManager
from joat.services.status import StatusPublisher
from joat.services.terminal_opening import TerminalOpeningController

ROOT = Path(__file__).resolve().parents[1]

class ObservedGateway(HubGatewayTransport):
    def __init__(self):
        super().__init__(host='127.0.0.1', port=18980)
        self.frames = []
    async def send(self, message):
        self.frames.append(message.to_wire())
        await super().send(message)

class NeverSender:
    async def send(self, *args):
        raise AssertionError('This acceptance never sends messages')

async def until(predicate, timeout=20):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.05)

def http(url, payload=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None,
                                 headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read()

async def run(out):
    out.mkdir(parents=True, exist_ok=True)
    (out/'receipt.json').unlink(missing_ok=True)
    for port in (18980,18981,5298,9998):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1',port))
    held_response = asyncio.Event(); held_response.set()
    gateway = ObservedGateway()
    processes = []
    calls = []
    screens = []
    runner = None
    runtime = None
    cert, key = out/'local-cert.pem', out/'local-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                    '-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1',
                    '-keyout',str(key),'-out',str(cert)], check=True, capture_output=True)
    key.chmod(0o600)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert,key)
    client_tls = ssl.create_default_context(cafile=str(cert))
    async def rpc(request):
        body = await request.json()
        if request.headers.get('Authorization') != 'Bearer simulator-only-token':
            return web.json_response({'error':'unauthorized'}, status=401)
        lane = request.match_info['lane']
        calls.append({'lane':lane,'method':body['method']})
        if body['method'] == 'tasks/get':
            return web.json_response({'jsonrpc':'2.0','id':body['id'],
                                     'error':{'code':-32001,'message':'Task not found'}})
        assert body['method'] == 'message/send'
        prompt = body['params']['message']['parts'][0]['text']
        assert f'Inspect {lane}' in prompt
        await asyncio.sleep(.5)
        await held_response.wait()
        return web.json_response({'jsonrpc':'2.0','id':body['id'],'result':{
            'id':f'{lane}-task','contextId':f'{lane}-conversation',
            'status':{'state':'TASK_STATE_COMPLETED'},
            'artifacts':[{'parts':[{'kind':'text','text':f'{lane} simulator reply: request received and completed.'}]}]}})
    app = web.Application(); app.router.add_post('/{lane}',rpc)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,'127.0.0.1',18981,ssl_context=server_tls).start()
    async def token(): return 'simulator-only-token'
    agents = {name:A2ATerminalAgent(f'https://127.0.0.1:18981/{name}',token,name,
                                   transport=A2AHttpTransport(ssl_context=client_tls))
              for name in ('OpenClaw','Hermes')}
    hud = HudWriter(gateway)
    async def relay(frame): await opening.paint(frame)
    async def paint(frame, request_id, selection_id):
        await hud.show_agent_terminal(AgentTerminalHudView(
            state=frame.state,focus=frame.topic,lanes=frame.lanes,
            activity='\n'.join(frame.lines),approvals=frame.approvals,reason=frame.reason,
            opening_request_id=request_id,opening_selection_id=selection_id))
    roster = AgentTerminalRoster(list(agents.items()) + [(name, agents['OpenClaw']) for name in ('work','calendar','hermes session')], paint=relay, visible_lanes=tuple(agents))
    opening = TerminalOpeningController(roster,paint=paint,
                                        connection_generation=lambda:gateway.audio_connection_generation)
    router = CapabilityRouter(hud,gateway,StatusPublisher(out/'status.json'),None,CardLog(),
                              terminal_rows=roster.picker_rows,terminal_busy=lambda:roster.busy,
                              terminal_current=lambda:roster.focused,terminal_open=opening.open)
    commands = AgentTerminalCommands(roster)
    async def capture():
        current = commands.begin_capture()
        assert current is not None
        assert await runtime.publish_terminal_capture(current)
    sessions = SessionManager(hud,lambda _:None,terminal_capture=capture,
                              terminal_view_current=lambda:opening.current)
    replies = MessageReplyController(NeverSender(),gateway.request_confirmation,contact_search=lambda _:[])
    runtime = PromptedSpeechRuntime(hud,replies,sessions=lambda:sessions,
                                    connection_generation=lambda:gateway.audio_connection_generation,
                                    fence_audio=lambda:10,capturing=lambda:False,terminal=lambda:commands)
    gateway.on_input(router.on_input)
    gateway.on_device(runtime.handle_device)
    gateway.on_brainstorm_action(sessions.handle_brainstorm_action)
    connected = asyncio.Event(); gateway.on_hello(lambda _:connected.set())
    async def input_action(action):
        await asyncio.to_thread(http,'http://127.0.0.1:9998/api/input',{'action':action})
        await asyncio.sleep(.7)
    async def screen(name):
        await asyncio.sleep(2.5)
        previous = None
        for _ in range(20):
            raw = await asyncio.to_thread(http,'http://127.0.0.1:9998/api/screenshot/glasses')
            visible = Image.open(io.BytesIO(raw)).crop((0,30,576,288)).tobytes()
            if visible == previous: break
            previous = visible
            await asyncio.sleep(.35)
        else: raise AssertionError('Simulator framebuffer did not settle')
        (out/f'{name}.rgba.png').write_bytes(raw)
        im=Image.open(io.BytesIO(raw)).convert('RGBA')
        bg=Image.new('RGBA',im.size,'black');bg.alpha_composite(im);bg.convert('RGB').save(out/f'{name}.png')
        screens.append({'name':name,'sha256':hashlib.sha256(raw).hexdigest(),'last_frame':gateway.frames[-1] if gateway.frames else None})
        print('SCREEN',name,flush=True)
    def start(name,args,cwd):
        log=(out/f'{name}.log').open('wb')
        proc=subprocess.Popen(args,cwd=cwd,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        processes.append(proc);log.close();return proc
    try:
        await gateway.start()
        env=dict(os.environ,VITE_MAC_WS_URL='ws://127.0.0.1:18980/glasses')
        subprocess.run([str(ROOT/'apps/hub-plugin/node_modules/.bin/vite'),'build','--outDir',str(out/'plugin'),'--emptyOutDir'],
                       cwd=ROOT/'apps/hub-plugin',env=env,check=True,stdout=(out/'build.log').open('w'),stderr=subprocess.STDOUT)
        start('server',[sys.executable,'-m','http.server','5298','--bind','127.0.0.1','--directory',str(out/'plugin')],ROOT)
        start('simulator',[str(Path.home()/'.local/bin/evenhub-simulator'),'http://127.0.0.1:5298','--automation-port','9998','--aid','coreaudio:BuiltInMicrophoneDevice'],ROOT)
        await asyncio.wait_for(connected.wait(),45);await asyncio.sleep(3)
        await screen('00-home')
        await input_action('context_menu'); await screen('01-feature-menu')
        for _ in range(5): await input_action('down')
        await screen('02-agent-terminal-selected')
        await input_action('click'); await screen('03-agent-picker')
        assert [name for name, _ in roster.picker_rows()] == list(agents)
        assert gateway.frames[-1]['kind'] == 'hud.list', gateway.frames[-1]
        assert [row.split(' · ')[0] for row in gateway.frames[-1]['items']] == list(agents)
        for index,name in enumerate(agents):
            if index: await input_action('down')
            await input_action('click')
            await until(lambda:roster.focused == name and agents[name].capture_context_generation is not None)
            await screen(f'{4+index*4:02}-{name.lower()}-conversation')
            await input_action('click')
            await until(lambda:runtime.dispatcher.is_armed)
            await screen(f'{5+index*4:02}-{name.lower()}-question')
            ticket=runtime.dispatcher.current_ticket
            assert ticket is not None
            accepted=await runtime.dispatcher.dispatch(Transcript(text=f'Inspect {name}',is_final=True),
                UtteranceProvenance(source=ticket.source,connection_generation=ticket.connection_generation,
                                    audio_epoch=ticket.prompt_id,first_ingress_ordinal=11,last_ingress_ordinal=12,speaker_roles=frozenset()))
            assert accepted, 'Acknowledged question did not accept the synthetic final STT result'
            await until(lambda: not roster.busy and gateway.frames[-1].get('kind') == 'agent_terminal.view' and f'{name} simulator reply:' in str(gateway.frames[-1]))
            await screen(f'{6+index*4:02}-{name.lower()}-reply')
            await input_action('double_click');await screen(f'{7+index*4:02}-{name.lower()}-back-picker')
            assert gateway.frames[-1]['kind'] == 'hud.list', gateway.frames[-1]
            assert [name for name, _ in roster.picker_rows()] == list(agents)
            assert [row.split(' · ')[0] for row in gateway.frames[-1]['items']] == list(agents)
        # A cancelled input returns to its selected conversation through simulator Back.
        await input_action('click')
        await until(lambda: opening.current and roster.focused == 'OpenClaw' and gateway.frames[-1].get('kind') == 'agent_terminal.view')
        await asyncio.sleep(3)
        await input_action('click'); await until(lambda: runtime.dispatcher.is_armed)
        await input_action('double_click'); await screen('12-question-cancelled')
        assert not runtime.dispatcher.is_armed
        assert roster.focused == 'OpenClaw'
        assert 'OpenClaw simulator reply:' in str(gateway.frames[-1])
        # Busy re-entry still exposes the same two-agent chooser.
        await input_action('click'); await until(lambda: runtime.dispatcher.is_armed)
        ticket = runtime.dispatcher.current_ticket
        held_response.clear()
        dispatch = asyncio.create_task(runtime.dispatcher.dispatch(Transcript(text=f'Inspect {roster.focused}',is_final=True),
            UtteranceProvenance(source=ticket.source,connection_generation=ticket.connection_generation,
                audio_epoch=ticket.prompt_id,first_ingress_ordinal=11,last_ingress_ordinal=12,speaker_roles=frozenset())))
        await until(lambda: roster.busy)
        await input_action('context_menu')
        for _ in range(5): await input_action('down')
        await input_action('click'); await screen('13-busy-agent-picker')
        assert gateway.frames[-1]['kind'] == 'hud.list'
        assert [row.split(' · ')[0] for row in gateway.frames[-1]['items']] == list(agents)
        held_response.set(); assert await dispatch
        (out/'receipt.json').write_text(json.dumps({'result':'passed','screens':screens,'rpc_calls':calls,
            'boundaries':['Local TLS A2A servers replace remote providers','Final STT text injected only after real simulator prompt acknowledgement'],
            'isolation':{'engine_port':18980,'agent_port':18981,'plugin_port':5298,'simulator_port':9998}},indent=2))
    finally:
        (out/'observed.json').write_text(json.dumps({'screens':screens,'rpc_calls':calls,'frames':gateway.frames},indent=2))
        for proc in reversed(processes):
            if proc.poll() is None:
                os.killpg(proc.pid,15)
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: os.killpg(proc.pid,9);proc.wait()
        if runtime: await runtime.aclose()
        await sessions.close()
        await gateway.stop()
        for agent in agents.values(): await agent.close()
        if runner: await runner.cleanup()
        key.unlink(missing_ok=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'dist/mac-workspace/control-audit/agent-terminal-isolated')
    args=parser.parse_args()
    asyncio.run(run(args.output.resolve()))
