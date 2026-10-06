"""The phone control screen's wire shapes and the actions behind them."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from joat.agents.cli_accounts import AccountProfile, AccountRegistry
from joat.agents.cli_signin import AccountStatus, LoginSnapshot
from joat.core.protocol import (
    ControlAction,
    ControlState,
    ProtocolError,
    parse_uplink,
)
from joat.services.brainstorm_control import BrainstormControl, BrainstormRoom
from joat.services.models import ModelRegistry, assistant_slot, default_slots


# ── protocol ─────────────────────────────────────────────────────────────────


def test_parse_reset_and_refresh() -> None:
    assert parse_uplink({"kind": "control.action", "action": "brainstorm.reset"}) == \
        ControlAction(action="brainstorm.reset")
    assert parse_uplink({"kind": "control.action", "action": "refresh"}).action == "refresh"  # type: ignore[union-attr]


def test_parse_account_actions() -> None:
    add = parse_uplink({"kind": "control.action", "action": "account.add",
                        "provider": "codex", "label": " Work "})
    assert add == ControlAction(action="account.add", provider="codex", label="Work")
    use = parse_uplink({"kind": "control.action", "action": "account.activate",
                        "provider": "claude", "profileId": "acct-0a1b2c3d"})
    assert use == ControlAction(action="account.activate", provider="claude", profile_id="acct-0a1b2c3d")


def test_pasted_code_never_reaches_repr() -> None:
    action = parse_uplink({"kind": "control.action", "action": "account.code", "code": " abc#123 "})
    assert isinstance(action, ControlAction)
    assert action.code == "abc#123"
    assert "abc#123" not in repr(action)


@pytest.mark.parametrize("payload", [
    {"kind": "control.action", "action": "rm -rf"},
    {"kind": "control.action", "action": "refresh", "extra": 1},
    {"kind": "control.action", "action": "account.add", "provider": "gemini", "label": "x"},
    {"kind": "control.action", "action": "account.activate", "provider": "codex", "profileId": "../etc"},
    {"kind": "control.action", "action": "account.code", "code": "a\nb" * 400},
    {"kind": "control.action", "action": "model.select", "slot": "stt", "option": "turbo"},
    {"kind": "control.action", "action": "account.add", "provider": "codex"},
])
def test_rejects_malformed_control_actions(payload: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        parse_uplink(payload)


# ── the service ─────────────────────────────────────────────────────────────


class FakeSignIn:
    def __init__(self) -> None:
        self.started: list[AccountProfile] = []
        self.codes: list[str] = []
        self.signed_out: list[str] = []
        self.current: LoginSnapshot | None = None
        self.statuses: dict[str, AccountStatus] = {}
        self.sign_out_ok = True
        self.cancels = 0

    def binary(self, provider: str) -> str | None:
        return f"/bin/{provider}"

    async def status(self, profile: AccountProfile) -> AccountStatus:
        return self.statuses.get(profile.profile_id, AccountStatus("logged_in", email="me@x.y"))

    async def sign_out(self, profile: AccountProfile) -> bool:
        self.signed_out.append(profile.profile_id)
        return self.sign_out_ok

    def start(self, profile: AccountProfile) -> LoginSnapshot:
        self.started.append(profile)
        self.current = LoginSnapshot(profile.provider, profile.profile_id, "open_link",
                                     url="https://auth.openai.com/codex/device", code="AAAA-BBBBB")
        return self.current

    def submit_code(self, code: str) -> bool:
        self.codes.append(code)
        return True

    def cancel(self, *, publish: bool = True) -> None:
        self.cancels += 1
        self.current = None

    def dismiss(self) -> None:
        self.current = None


class Harness:
    def __init__(self, root: Path) -> None:
        self.registry = AccountRegistry(root)
        self.sent: list[ControlState] = []
        self.refreshes = 0
        self.resets = 0
        self.warms = 0
        self.signin = FakeSignIn()
        self.models = ModelRegistry([*default_slots(), assistant_slot("claude")])

        async def send(state: ControlState) -> None:
            self.sent.append(state)

        async def reset() -> bool:
            self.resets += 1
            return True

        async def warm() -> None:
            self.warms += 1

        def refresh() -> None:
            self.refreshes += 1

        self.control = BrainstormControl(
            registry=self.registry, models=self.models, send=send,
            room=lambda: BrainstormRoom("ready", "idle", "Codex and Claude are seated.",
                                        (("codex", True), ("claude", True))),
            refresh_seats=refresh, reset_room=reset, warm_seats=warm,
            observed=lambda: {"claude": "limited"},
            signin=self.signin,  # type: ignore[arg-type]
        )


def test_reset_stops_room_refreshes_and_publishes(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    asyncio.run(h.control.apply(ControlAction(action="brainstorm.reset")))
    assert (h.resets, h.refreshes, h.warms) == (1, 1, 1)
    state = h.sent[-1]
    assert state.notice == "Brainstorm stopped and reset."
    wire = state.to_wire()
    assert wire["kind"] == "control.state"
    seats = {s["name"]: s for s in wire["brainstorm"]["seats"]}
    # A live usage-limit observation outranks the last probe.
    assert seats["claude"]["account"] == "limited"
    assert seats["codex"]["account"] == "logged_in"


def test_add_account_starts_sign_in_and_shows_code(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    asyncio.run(h.control.apply(ControlAction(action="account.add", provider="codex", label="Work")))
    assert [p.label for p in h.signin.started] == ["Work"]
    login = h.sent[-1].to_wire()["login"]
    assert login["code"] == "AAAA-BBBBB" and login["url"].startswith("https://")


def test_switching_account_rebuilds_and_warms_seats(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    work = h.registry.add("claude", "Work")
    asyncio.run(h.control.apply(ControlAction(
        action="account.activate", provider="claude", profile_id=work.profile_id)))
    assert h.registry.active("claude") == work
    assert h.refreshes == 1 and h.warms == 1
    accounts = {a["provider"]: a for a in h.sent[-1].to_wire()["accounts"]}
    active = [p for p in accounts["claude"]["profiles"] if p["active"]]
    assert [p["label"] for p in active] == ["Work"]


def test_remove_signs_out_first_and_deletes_only_its_home(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    extra = h.registry.add("codex", "Spare")
    assert extra.home is not None
    asyncio.run(h.control.apply(ControlAction(
        action="account.remove", provider="codex", profile_id=extra.profile_id)))
    assert h.signin.signed_out == [extra.profile_id]
    assert not Path(extra.home).exists()
    assert (tmp_path / "accounts.json").exists()


def test_model_select_uses_registry(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    asyncio.run(h.control.apply(ControlAction(action="model.select", slot="codex", option="spark")))
    assert h.models.slot("codex").current.label == "spark"  # type: ignore[union-attr]
    assert "spark" in h.sent[-1].notice


def test_unknown_account_is_a_notice_not_a_crash(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    asyncio.run(h.control.apply(ControlAction(
        action="account.activate", provider="codex", profile_id="acct-00000000")))
    assert h.sent[-1].notice == "That account no longer exists."


# ── 2026-10-06: races between a sign-in and the account it is for ────────────


def test_remove_cancels_a_sign_in_still_running_for_that_account(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    async def scenario() -> None:
        await h.control.apply(ControlAction(action="account.add", provider="codex", label="Spare"))
        spare = h.signin.started[-1]
        assert h.signin.current is not None
        await h.control.apply(ControlAction(
            action="account.remove", provider="codex", profile_id=spare.profile_id))

    asyncio.run(scenario())
    assert h.signin.cancels == 1 and h.signin.current is None


def test_remove_leaves_another_accounts_sign_in_alone(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    spare = h.registry.add("codex", "Spare")

    async def scenario() -> None:
        await h.control.apply(ControlAction(action="account.add", provider="claude", label="Work"))
        await h.control.apply(ControlAction(
            action="account.remove", provider="codex", profile_id=spare.profile_id))

    asyncio.run(scenario())
    assert h.signin.cancels == 0 and h.signin.current is not None


def test_remove_keeps_an_account_whose_sign_out_failed(tmp_path: Path) -> None:
    """Deleting the home of an account the CLI is still signed in to
    orphaned that login where nothing could revoke it."""
    h = Harness(tmp_path)
    spare = h.registry.add("claude", "Spare")
    h.signin.sign_out_ok = False
    asyncio.run(h.control.apply(ControlAction(
        action="account.remove", provider="claude", profile_id=spare.profile_id)))
    assert h.registry.get("claude", spare.profile_id) == spare
    assert spare.home is not None and Path(spare.home).exists()
    assert "was kept" in h.sent[-1].notice


def test_remove_proceeds_when_sign_out_failed_but_nobody_is_signed_in(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    spare = h.registry.add("claude", "Spare")
    h.signin.sign_out_ok = False
    h.signin.statuses[spare.profile_id] = AccountStatus("logged_out")
    asyncio.run(h.control.apply(ControlAction(
        action="account.remove", provider="claude", profile_id=spare.profile_id)))
    assert all(p.profile_id != spare.profile_id for p in h.registry.profiles("claude"))


def test_a_sign_in_finishing_for_a_removed_account_is_a_notice(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    spare = h.registry.add("codex", "Spare")

    async def scenario() -> None:
        await h.control.apply(ControlAction(
            action="account.remove", provider="codex", profile_id=spare.profile_id))
        h.control._on_signed_in(spare)
        for _ in range(20):
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert "was removed before its sign-in finished" in h.sent[-1].notice


def test_a_change_during_a_slow_send_is_sent_after_it(tmp_path: Path) -> None:
    """The snapshot is taken when a send starts; a change landing while the
    phone is slow to take it used to be dropped (a device code with it)."""
    h = Harness(tmp_path)
    release = asyncio.Event()
    notices: list[str] = []

    async def slow_send(state: ControlState) -> None:
        notices.append(state.notice)
        await release.wait()

    h.control._send = slow_send  # type: ignore[assignment]

    async def scenario() -> None:
        h.control._notice = "first"
        h.control.publish_soon()
        await asyncio.sleep(0)
        h.control._notice = "second"
        h.control.publish_soon()
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert notices == ["first", "second"]
