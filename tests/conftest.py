"""Test-wide safety: nothing here may write into the wearer's home.

`BrainstormHistory` defaults its export directory to
`~/Documents/JOAT Brainstorms`, deliberately — "the point of an export is
that the wearer can find it". Tests that passed `path=tmp_path/...` and
said nothing about `export_dir` inherited that default and wrote real
files there. One run of test_brainstorm_history.py deposited THIRTEEN,
and 312 had accumulated: the wearer's genuine sessions buried among
fixtures with topics like "tabs or spaces".

Fixing the call sites would only fix today's. This makes the mistake
unreachable, so a test added next month cannot reintroduce it by
forgetting an argument — the same reasoning as bounding a HUD frame where
it is built rather than at every caller.

Deliberately narrow. Other services with `Path.home()` defaults compute
them per call rather than at import, and were measured writing nothing:
running the voice-note, note-command, diagnostics and voice-latency
suites changed the file count under both JOAT directories by zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from joat.services import brainstorm_history


@pytest.fixture(autouse=True)
def _keep_tests_out_of_the_wearers_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        brainstorm_history, "DEFAULT_EXPORT_DIR", tmp_path / "JOAT Brainstorms"
    )
    monkeypatch.setattr(
        brainstorm_history, "DEFAULT_HISTORY_PATH", tmp_path / "brainstorms.json"
    )
