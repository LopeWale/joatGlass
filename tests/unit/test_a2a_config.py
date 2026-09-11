import json
from pathlib import Path

import pytest

from joat.agents.a2a_config import A2AProfile, load_a2a_profiles, native_token_loader


def write_profiles(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"profiles": entries}))
    path.chmod(0o600)


def entry(**changes: object) -> dict[str, object]:
    return {"id": "openclaw", "label": "OpenClaw", "endpoint": "https://host.example:8443/a2a/v1", "enabled": True, **changes}


def test_missing_configuration_is_empty(tmp_path: Path) -> None:
    assert load_a2a_profiles(tmp_path / "missing") == []


def test_loads_enabled_and_disabled_profiles_without_credentials(tmp_path: Path) -> None:
    path = tmp_path / "a2a.json"
    write_profiles(path, [entry(), entry(id="hermes", label="Hermes", endpoint="https://host.example:9900/", enabled=False)])
    profiles = load_a2a_profiles(path)
    assert [p.id for p in profiles] == ["openclaw", "hermes"]
    assert profiles[1].enabled is False


@pytest.mark.parametrize("changes", [
    {"endpoint": "http://host.example/a2a"},
    {"endpoint": "https://user:password@host.example/a2a"},
    {"endpoint": "https://host.example/a2a?token=secret"},
    {"endpoint": "https://host.example/a2a#fragment"},
    {"endpoint": "https://host.example/\npath"},
    {"endpoint": "https://host.example:bad/a2a"},
    {"token": "must-never-be-in-config"},
    {"id": "../../bad"},
    {"enabled": "false"},
    {"label": "bad\nlabel"},
])
def test_invalid_profile_is_rejected(tmp_path: Path, changes: dict[str, object]) -> None:
    path = tmp_path / "a2a.json"
    write_profiles(path, [entry(**changes)])
    with pytest.raises(ValueError):
        load_a2a_profiles(path)


def test_duplicate_and_symlink_config_rejected(tmp_path: Path) -> None:
    path = tmp_path / "a2a.json"
    write_profiles(path, [entry(), entry()])
    with pytest.raises(ValueError):
        load_a2a_profiles(path)
    write_profiles(path, [entry()])
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_a2a_profiles(link)


async def test_native_loader_binds_token_to_exact_profile_and_destination() -> None:
    requests: list[tuple[str, object]] = []

    class Result:
        result = {"token": "test-only-token"}

    class Bridge:
        async def call(self, method: str, body: object = None) -> Result:
            requests.append((method, body))
            return Result()

    profile = A2AProfile("openclaw", "OpenClaw", "https://host.example:8443/a2a/v1", True)
    loader = native_token_loader(profile, Bridge())
    assert await loader() == "test-only-token"
    assert requests == [("a2a.token.load", {"profile_id": "openclaw", "endpoint": profile.endpoint})]
