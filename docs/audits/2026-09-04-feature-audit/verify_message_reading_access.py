"""Messages reading, action ownership and access recovery regression gate; synthetic stores only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / 'evidence/remediation'
TESTS = [
    'tests/integration/test_message_access_recovery.py', 'tests/integration/test_message_ui.py',
    'tests/integration/test_message_reply_entry.py', 'tests/integration/test_prompted_speech.py',
    'tests/integration/test_prompted_speech_runtime.py', 'tests/integration/test_prompt_gateway.py',
    'tests/unit/test_imessage.py', 'tests/unit/test_capabilities.py',
    'tests/unit/test_builtin_tools.py', 'tests/unit/test_notification_mux.py',
    'tests/unit/test_message_reply.py', 'tests/unit/test_message_tools.py',
    'tests/unit/test_message_prompts.py', 'tests/unit/test_prompted_input.py',
    'tests/unit/test_reader.py', 'tests/unit/test_protocol.py',
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', default='message-reading-access')
    args = parser.parse_args()
    if not args.label or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in args.label):
        parser.error('Use a simple run label.')
    manifest_path = OUT / f'{args.label}-candidate.json'
    frozen = json.loads(manifest_path.read_text())
    receipt = OUT / f'{args.label}-gate-receipt.json'
    if receipt.exists():
        raise SystemExit('Retain prior receipt; a repeated run requires changed evidence.')
    result = dict(scope=frozen['scope'], candidate_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                  checks=[], source_changed=[], simulator_visuals=False, physical_g2=False,
                  production_database_opened=False, full_engine_startup_verified=False)

    def save() -> None:
        result['source_changed'] = [p for p, h in frozen['source_sha256'].items()
                                   if not (ROOT / p).is_file() or hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h]
        receipt.write_text(json.dumps(result, indent=2) + '\n')

    commands = [
        ('python', [str(ROOT / '.venv/bin/python'), '-m', 'pytest', '-q', *TESTS, '--durations=10',
                    '--junitxml=' + str(OUT / f'{args.label}-gate.xml')], ROOT),
        ('mypy', [str(ROOT / '.venv/bin/python'), '-m', 'mypy'], ROOT),
        ('hub-tests', ['npm', 'test', '--', '--run', 'src/MessageUI.test.ts', 'src/MessagesViewState.test.ts',
                       'src/CapabilityMenu.test.ts', 'src/CapabilityMenu.panel.test.ts',
                       'src/HudPresenter.nativeList.test.ts', 'src/HudCanvasRenderer.test.ts',
                       'src/EventRouter.test.ts'], ROOT / 'apps/hub-plugin'),
        ('hub-build', ['npm', 'run', 'build'], ROOT / 'apps/hub-plugin'),
    ]
    for name, argv, cwd in commands:
        save()
        if result['source_changed']:
            raise SystemExit('Source changed before verification.')
        start = time.monotonic()
        with (OUT / f'{args.label}-gate-{name}.log').open('x') as log:
            child = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                status: int | str = child.wait(timeout=240)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=3)
                status = 'timeout'
        check = dict(name=name, exit_code=status, seconds=round(time.monotonic()-start, 3))
        result['checks'].append(check)
        save()
        print(json.dumps(check), flush=True)
        if status != 0 or result['source_changed']:
            raise SystemExit(1)
    result['all_runs_passed'] = True
    save()


if __name__ == '__main__':
    main()
