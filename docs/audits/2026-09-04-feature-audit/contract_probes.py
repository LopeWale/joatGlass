"""Execute Mac -> wire -> Hub -> Mac audit probes with synthetic state only."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "evidence"
sys.path[:0] = [str(REPO / "apps/mac"), str(REPO / "scripts")]
from joat.core.protocol import HudList, InputEvent
from joat.services.capabilities import CapabilityRouter
from simulator_brainstorm_action_flow_acceptance import _prepare_simulator_plugin_root

spec = importlib.util.spec_from_file_location("audit_test_helpers", REPO / "tests/unit/test_capabilities.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)

TS = r'''
import { it, expect } from 'vitest';
import fs from 'node:fs';
import { CapabilityMenu } from './CapabilityMenu';
import { OsEventTypeList as E } from '@evenrealities/even_hub_sdk';
import { messagesFullTextContent, messagesReaderBodyLines, MESSAGES_READER_BODY_LINE_CAPACITY } from './HudCanvasRenderer';
const fixture = JSON.parse(fs.readFileSync(process.env.AUDIT_FIXTURE!, 'utf8'));
const result: any = {};
function rig() {
  const lists: any[] = [], sent: any[] = [];
  const hud: any = {
    showList: async (items: string[], options: any) => {lists.push({items, ...options}); return true;},
    setStatus: async () => true, setSelection: async () => true,
    moveSelection: async () => true, showDefault: async () => true,
  };
  return {menu: new CapabilityMenu(hud, {} as any, {send: (x: any) => sent.push(x)} as any), lists, sent};
}
async function present(menu: CapabilityMenu, frame: any) {
  expect(await menu.presentList(frame.title, frame.items, frame.requestId, frame.selectable, frame.hint)).toBe(true);
}
const click = (i: number) => ({eventType:E.CLICK_EVENT, currentSelectItemIndex:i}) as any;
it('records actual cross-boundary failures using Mac-authored wire frames', async () => {
  const q = rig();
  await present(q.menu, fixture.noteRecording);
  q.menu.handleList(click(2));
  await present(q.menu, fixture.noteDiscard);
  await q.menu.toggle();
  q.menu.handleScroll(E.SCROLL_TOP_EVENT); q.menu.handleScroll(E.SCROLL_TOP_EVENT);
  q.menu.handleList(click(0));
  const qevent = q.sent.filter(x => x.event?.containerName === 'capdata').at(-1).event;
  result.quicknotes = {restoredTitle:q.lists.at(-1).title, displayedRow:q.lists.at(-1).items[0], event:qevent};
  expect(result.quicknotes.displayedRow).toBe('Save and finish');
  expect(qevent.selectedIndex).toBe(0);
  await q.menu.close();

  const m = rig();
  await present(m.menu, fixture.messagePicker);
  m.menu.handleList(click(1));
  await present(m.menu, fixture.messageHistory);
  for (let i=0;i<20;i++) m.menu.handleScroll(E.SCROLL_BOTTOM_EVENT);
  result.messagePaging = {macHint:fixture.messageHistory.hint, requests:m.sent.filter(x => x.event?.containerName === 'cappage').length};
  expect(result.messagePaging.requests).toBe(0);
  await m.menu.toggle();
  const lifecycle = m.sent.filter(x => x.event?.containerName === 'capability-lifecycle').at(-1).event;
  m.menu.handleList(click(1));
  result.messageBack = {restoredRows:m.lists.at(-1).items.length, lifecycle,
    event:m.sent.filter(x=>x.event?.containerName === 'capdata').at(-1).event};
  await m.menu.close();

  const body=messagesFullTextContent(fixture.reader.items).body;
  const lines=messagesReaderBodyLines(body);
  result.reader={macPages:fixture.readerPageCount, wireBodyChars:body.length,
    wrappedLines:lines.length, visibleLineCapacity:MESSAGES_READER_BODY_LINE_CAPACITY, hint:fixture.reader.hint};
  expect(lines.length).toBe(11);
  expect(fixture.readerPageCount).toBe(1);
  expect(fixture.reader.hint.includes('down more')).toBe(false);
  fs.writeFileSync(process.env.AUDIT_RESULT!, JSON.stringify(result,null,2));
});
'''


async def pump(router, event):
    router.on_input(InputEvent(container_name=event.get("containerName"),
                               selected_name=event.get("selectedName"),
                               selected_index=event.get("selectedIndex"),
                               request_id=event.get("requestId")))
    await asyncio.sleep(.05)


def last_list(transport):
    return [x.to_wire() for x in transport.sent if isinstance(x, HudList)][-1]


async def main():
    with tempfile.TemporaryDirectory(prefix="joat-contract-audit-") as scratch:
        scratch = Path(scratch)
        capture = helpers.FakeVoiceCapture(state="recording", title="Synthetic note")
        notes, nt, _ = helpers.make_router(scratch, voice_capture=capture)
        await notes.open("QuickNotes", request_id="voice-audit-notes")
        fixtures = {"noteRecording": last_list(nt)}
        await pump(notes, {"containerName":"capdata", "selectedName":"QUICKNOTES", "selectedIndex":2, "requestId":"voice-audit-notes"})
        fixtures["noteDiscard"] = last_list(nt)

        replies=helpers.FakeReplies()
        messages, mt, _ = helpers.make_router(scratch, rows=[("Dele","Synthetic",1)], replies=replies)
        messages._messages=helpers.PagedHistoryMessages()
        await messages.open("Messages", request_id="voice-audit-messages")
        fixtures["messagePicker"]=last_list(mt)
        await pump(messages,{"containerName":"capdata","selectedName":"MESSAGES","selectedIndex":1,"requestId":"voice-audit-messages"})
        fixtures["messageHistory"]=last_list(mt)

        text=" ".join(["x"*27]*11)
        pages=CapabilityRouter._message_reader_pages(text)
        fixtures["readerPageCount"]=len(pages)
        fixtures["reader"]=HudList(items=["Synthetic · 1/1",*CapabilityRouter._message_reader_pages(pages[0],52)],
                                   title="MESSAGES · MESSAGE",hint="say reply · left x2: back").to_wire()
        fixture_path=scratch/'fixture.json';fixture_path.write_text(json.dumps(fixtures))
        result_path=scratch/'hub-result.json'
        root=_prepare_simulator_plugin_root(REPO,scratch,"ws://127.0.0.1:18895/glasses")
        (root/'src/audit-contract.test.ts').write_text(TS)
        env={**os.environ,"AUDIT_FIXTURE":str(fixture_path),"AUDIT_RESULT":str(result_path)}
        with (OUT/'contract-probes.log').open('w') as log:
            done=subprocess.run([str(REPO/'apps/hub-plugin/node_modules/.bin/vitest'),'run','src/audit-contract.test.ts'],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=30)
        if done.returncode: raise RuntimeError('Hub contract probe failed; inspect contract-probes.log')
        result=json.loads(result_path.read_text())
        await pump(notes,result['quicknotes']['event'])
        result['quicknotes']['fakeCaptureCalls']=capture.calls
        assert capture.calls==['discard','confirm_discard']
        await pump(messages,result['messageBack']['lifecycle'])
        before=len(mt.sent)
        await pump(messages,result['messageBack']['event'])
        result['messageBack']['macRowsAfterBack']=len(messages._message_rows)
        result['messageBack']['downlinksOnReselection']=len(mt.sent)-before
        assert len(messages._message_rows)==0 and len(mt.sent)==before
        result['scope']='Production Mac/Hub controller and wire integration; stub HUD paint, synthetic data, fake note capture; no real recording or message changed.'
        (OUT/'contract-probes.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))


if __name__=='__main__':
    asyncio.run(main())
