from __future__ import annotations

import os
import json
from pathlib import Path

import pytest

from joat.services.status import StatusPublisher


def test_status_projection_and_directory_are_owner_only(tmp_path: Path) -> None:
    directory = tmp_path / "support"
    path = directory / "status.json"

    status = StatusPublisher(path=path)
    status.set(last_transcript="private words")

    assert os.stat(directory).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert "private words" in path.read_text()


def test_personal_data_status_is_fixed_and_rejects_diagnostic_text(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    status = StatusPublisher(path=path)

    status.set_personal_data(state="unavailable")

    snapshot = json.loads(path.read_text())
    assert snapshot["personal_data"] == {"state": "unavailable"}
    with pytest.raises(ValueError, match="personal-data bridge state"):
        status.set_personal_data(state="socket /private/path: leaked reminder")
