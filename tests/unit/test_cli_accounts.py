"""Saved CLI accounts, discovery of a moved Codex, and failure classification."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from joat.agents.cli_accounts import AccountError, AccountRegistry, parse_provider
from joat.agents.cli_failures import classify_cli_failure
from joat.agents.cli_locate import find_codex_binary
from joat.agents.cli_signin import (
    LoginSnapshot,
    _advance,
    parse_claude_status,
    parse_codex_status,
    strip_terminal,
)
from joat.services.roundtable import (
    LIMIT_DETAIL,
    SIGNED_OUT_DETAIL,
    failure_notice,
    failure_summary,
)


def _exe(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ── discovery ────────────────────────────────────────────────────────────────


def test_codex_found_inside_chatgpt_bundle_by_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOAT_CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    app = tmp_path / "ChatGPT.app"
    cli = app / "Contents/Resources/codex-cli"
    _exe(cli / "bin/codex")
    (cli / "codex-package.json").write_text(json.dumps({"entrypoint": "bin/codex"}))
    found = find_codex_binary(app_roots=[app], extra_dirs=[])
    assert found == str(cli / "bin/codex")


def test_dangling_symlink_on_path_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-10-03 failure: ~/.local/bin/codex pointed at a moved file."""
    monkeypatch.delenv("JOAT_CODEX_BIN", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    os.symlink(tmp_path / "gone/codex", bindir / "codex")
    monkeypatch.setenv("PATH", str(bindir))
    app = tmp_path / "ChatGPT.app"
    legacy = _exe(app / "Contents/Resources/codex")
    assert find_codex_binary(app_roots=[app], extra_dirs=[bindir]) == str(legacy)


def test_codex_missing_everywhere_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOAT_CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert find_codex_binary(app_roots=[tmp_path / "nope.app"], extra_dirs=[]) is None


def test_explicit_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chosen = _exe(tmp_path / "custom/codex")
    monkeypatch.setenv("JOAT_CODEX_BIN", str(chosen))
    assert find_codex_binary(app_roots=[], extra_dirs=[]) == str(chosen)


# ── failures ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("output", [
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "Login expired · Please run /login",
    "Not logged in · Please run /login",
])
def test_signed_out_wordings(output: str) -> None:
    assert classify_cli_failure(output) == "signed_out"


def test_limit_wording() -> None:
    assert classify_cli_failure("You're out of usage credits") == "limit"


def test_mcp_oauth_noise_is_not_a_signed_out_seat() -> None:
    noise = "mcp server cloudflare: 401 Unauthorized (token expired, refresh token rejected)"
    assert classify_cli_failure(noise) == "other"


def test_failure_summary_names_only_actionable_causes() -> None:
    assert failure_summary(failure_notice("claude", SIGNED_OUT_DETAIL)) == SIGNED_OUT_DETAIL
    assert failure_summary(failure_notice("codex", LIMIT_DETAIL)) == LIMIT_DETAIL
    assert failure_summary(failure_notice("codex", "exited 1 — see the log")) == "could not answer"


# ── registry ─────────────────────────────────────────────────────────────────


def test_default_account_is_the_cli_default_home(tmp_path: Path) -> None:
    registry = AccountRegistry(tmp_path)
    assert registry.active("claude").is_default
    # None REMOVES the variable, so a stray inherited value cannot leak in.
    assert registry.env_overlay("claude") == {"CLAUDE_CONFIG_DIR": None}
    assert registry.env_overlay("codex") == {"CODEX_HOME": None}


def test_add_activate_persist_and_reload(tmp_path: Path) -> None:
    registry = AccountRegistry(tmp_path)
    work = registry.add("codex", "  Work   account ")
    assert work.label == "Work account"
    assert work.home is not None and Path(work.home).is_dir()
    assert stat.S_IMODE(Path(work.home).stat().st_mode) == 0o700
    registry.activate("codex", work.profile_id)
    assert registry.env_provider("codex")() == {"CODEX_HOME": work.home}
    reloaded = AccountRegistry(tmp_path)
    assert reloaded.active("codex") == work
    saved = json.loads((tmp_path / "accounts.json").read_text())
    assert "token" not in json.dumps(saved).lower()
    assert stat.S_IMODE((tmp_path / "accounts.json").stat().st_mode) == 0o600


def test_remove_active_falls_back_to_default(tmp_path: Path) -> None:
    registry = AccountRegistry(tmp_path)
    extra = registry.add("claude", "Personal")
    registry.activate("claude", extra.profile_id)
    registry.remove("claude", extra.profile_id)
    assert registry.active("claude").is_default
    with pytest.raises(AccountError):
        registry.remove("claude", "default")


def test_hand_edited_home_outside_root_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "accounts.json").write_text(json.dumps({
        "claude": {"active": "acct-deadbeef", "profiles": [
            {"profile_id": "acct-deadbeef", "label": "Evil", "home": "/etc"},
        ]},
    }))
    registry = AccountRegistry(tmp_path)
    assert [p.profile_id for p in registry.profiles("claude")] == ["default"]
    assert registry.active("claude").is_default


def test_parse_provider() -> None:
    assert parse_provider("codex") == "codex"
    with pytest.raises(AccountError):
        parse_provider("gemini")


# ── sign-in parsing (shapes captured from the real CLIs, 2026-10-05) ───────


CODEX_DEVICE = (
    "\r\nWelcome to Codex [v\x1b[90m0.160.0\x1b[0m]\r\n"
    "Follow these steps to sign in with ChatGPT using device code authorization:\r\n\r\n"
    "1. Open this link in your browser and sign in to your account\r\n"
    "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\r\n\r\n"
    "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\r\n"
    "   \x1b[94mGBEA-VS5GL\x1b[0m\r\n"
)

CLAUDE_LOGIN = (
    "Opening browser to sign in…\r\nIf the browser didn't open, visit: "
    "\x1b]8;;https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y\x07"
    "\x1b[94mhttps://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y\x1b[39m"
    "\x1b]8;;\x07\r\nPaste code here if prompted > "
)


def test_codex_device_flow_yields_link_and_code() -> None:
    snapshot = _advance(LoginSnapshot("codex", "default", "starting"), strip_terminal(CODEX_DEVICE))
    assert snapshot.stage == "open_link"
    assert snapshot.url == "https://auth.openai.com/codex/device"
    assert snapshot.code == "GBEA-VS5GL"
    assert not snapshot.needs_code


def test_claude_flow_asks_for_pasted_code() -> None:
    snapshot = _advance(LoginSnapshot("claude", "default", "starting"), strip_terminal(CLAUDE_LOGIN))
    assert snapshot.stage == "enter_code"
    assert snapshot.needs_code
    assert snapshot.url == "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y"


def test_status_parsers() -> None:
    assert parse_claude_status('{"loggedIn": false, "authMethod": "none"}').state == "logged_out"
    signed = parse_claude_status('{"loggedIn": true, "email": "a@b.c", "subscriptionType": "max"}')
    assert (signed.state, signed.email, signed.plan) == ("logged_in", "a@b.c", "max")
    assert parse_codex_status("Logged in using ChatGPT").state == "logged_in"
    assert parse_codex_status("Not logged in").state == "logged_out"
    assert parse_claude_status("garbage").state == "unknown"


def test_a_saved_claude_account_does_not_run_as_an_inherited_login_token(tmp_path: Path) -> None:
    """2026-10-06: CLAUDE_CODE_OAUTH_TOKEN in the brain's environment
    outranks CLAUDE_CONFIG_DIR, so switching to a saved account did nothing."""
    registry = AccountRegistry(tmp_path)
    work = registry.add("claude", "Work")
    overlay = registry.env_overlay("claude", work)
    assert overlay["CLAUDE_CONFIG_DIR"] == work.home
    assert overlay["CLAUDE_CODE_OAUTH_TOKEN"] is None and overlay["ANTHROPIC_AUTH_TOKEN"] is None
    default = registry.env_overlay("claude", registry.get("claude", "default"))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in default, "the Mac's own login is left as configured"
