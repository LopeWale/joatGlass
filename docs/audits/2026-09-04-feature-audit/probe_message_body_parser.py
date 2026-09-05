"""Pinned public fixtures and a data-only parser candidate. No live Messages access."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / 'vendor/joat-message-parser-probe'
OUT = Path(__file__).resolve().parent / 'evidence/remediation/message-body-parser-probe.json'
WHEEL = 'https://files.pythonhosted.org/packages/0d/1a/dbbbad9690c24506aec4571bc6613a5625b163b1607777109c9dabdbec07/pytypedstream-0.1.0-py3-none-any.whl'
WHEEL_HASH = '499920d4cb8bec8fd9d9cdd4c6312765eb418c398c10371ab1c9c0051104d278'
UPSTREAM = '4d90fc8d20a745c0a8acc1e01c0631c4bab89cb4'
NAMES = ['AttributedBodyTextOnly', 'AttributedBodyTextOnly2', 'LongMessage',
         'EmojiBoldUnderline', 'Formatted', 'MultiPart', 'SingleLink', 'Blank', 'WeirdText']
EXPECTED = {'AttributedBodyTextOnly': 'Noter test', 'AttributedBodyTextOnly2': 'Test 3'}


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        value = response.read(2_000_001)
    if len(value) > 2_000_000:
        raise ValueError('Public fixture exceeded probe download limit')
    return value


def main() -> None:
    if OUT.exists() or WORK.exists():
        raise SystemExit('Retain previous evidence; do not overwrite this probe.')
    WORK.mkdir(parents=True)
    receipt: dict = {'upstream_fixture_commit': UPSTREAM, 'parser_distribution': 'pytypedstream==0.1.0',
        'parser_wheel_sha256': WHEEL_HASH, 'samples': [], 'live_messages_read': False,
        'runtime_dependency_installed': False, 'simulator_verified': False}
    try:
        wheel = download(WHEEL)
        if hashlib.sha256(wheel).hexdigest() != WHEEL_HASH:
            raise ValueError('Parser distribution hash mismatch')
        wheel_path = WORK / 'pytypedstream-0.1.0-py3-none-any.whl'
        wheel_path.write_bytes(wheel)
        # Import directly from the intact pinned wheel, retaining its license
        # metadata; no installation into the running application's environment.
        sys.path.insert(0, str(wheel_path))
        import typedstream
        from typedstream.archiving import GenericArchivedObject, TypedValue
        from typedstream.types.foundation import NSString
        blobs = []
        for name in NAMES:
            blob = download(f'https://raw.githubusercontent.com/ReagentX/imessage-exporter/{UPSTREAM}/imessage-database/test_data/typedstream/{name}')
            (WORK / name).write_bytes(blob)
            blobs.append(blob)
            item = {'fixture': name, 'blob_bytes': len(blob), 'blob_sha256': hashlib.sha256(blob).hexdigest()}
            start = time.perf_counter()
            try:
                root = typedstream.unarchive_from_data(blob)
                if not isinstance(root, GenericArchivedObject):
                    raise ValueError('Root is not a generic attributed string')
                item['root_class'] = root.clazz.name.decode('ascii')
                if root.clazz.name not in (b'NSAttributedString', b'NSMutableAttributedString'):
                    raise ValueError('Unexpected archived root class')
                first = root.contents[0]
                if not isinstance(first, TypedValue) or not isinstance(first.value, NSString):
                    raise ValueError('First field is not a single NSString')
                value = first.value.value
                if not isinstance(value, str):
                    raise ValueError('NSString has no decoded string value')
                item.update(decoded=True, characters=len(value), text_sha256=hashlib.sha256(value.encode()).hexdigest())
                if name in EXPECTED:
                    item['matches_upstream_expected_text'] = value == EXPECTED[name]
            except Exception as exc:
                item.update(decoded=False, error_type=type(exc).__name__, error=str(exc)[:160])
            item['milliseconds'] = round((time.perf_counter() - start) * 1000, 3)
            receipt['samples'].append(item)
        # Same public-format blobs inserted into a synthetic Messages schema.
        # Reproduce the existing query omission independently of decoder success.
        path = WORK / 'synthetic-chat.db'
        with sqlite3.connect(path) as db:
            db.executescript('''
                CREATE TABLE message (handle_id INTEGER, text TEXT, attributedBody BLOB, date INTEGER, is_from_me INTEGER);
                CREATE TABLE handle (id TEXT);
                CREATE TABLE chat (display_name TEXT);
                CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
                CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
                INSERT INTO handle VALUES ('synthetic-participant');
                INSERT INTO chat VALUES ('Parser fixture');
                INSERT INTO chat_handle_join VALUES (1, 1);
            ''')
            for i, blob in enumerate(blobs, 1):
                db.execute('INSERT INTO message VALUES (1, NULL, ?, ?, 0)', (blob, i))
                db.execute('INSERT INTO chat_message_join VALUES (?, 1)', (i,))
        sys.path.insert(0, str(ROOT / 'apps/mac'))
        from joat.channels.imessage import IMessageStore, MessageThread
        store = IMessageStore(path)
        store._contacts._map = {}
        thread = MessageThread('Parser fixture', 'synthetic-participant', '', 0.0, chat_id=1)
        receipt['production_before'] = {'attributed_body_rows': len(blobs),
            'visible_conversations': len(store.recent_threads(10)),
            'visible_history_entries': len(store.thread_message_page(thread, limit=16).entries),
            'new_notifications': len(store.read_new(0)[0]),
            'imessage_source_sha256': hashlib.sha256((ROOT / 'apps/mac/joat/channels/imessage.py').read_bytes()).hexdigest()}
        receipt['experiment_completed'] = True
    finally:
        OUT.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
