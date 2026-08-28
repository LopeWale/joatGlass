from __future__ import annotations

import os
from pathlib import Path

from joat.services.status import StatusPublisher


def test_status_projection_and_directory_are_owner_only(tmp_path: Path) -> None:
    directory = tmp_path / "support"
    path = directory / "status.json"

    status = StatusPublisher(path=path)
    status.set(last_transcript="private words")

    assert os.stat(directory).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert "private words" in path.read_text()
