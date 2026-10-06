"""The phone's JOAT control screen: Brainstorm reset, accounts, seat models.

The glasses are for the conversation; the phone is where the wearer
configures it. Everything here is driven by ``control.action`` from the
plugin's phone screen and answered with one whole ``control.state``.

Owns no CLI mechanics: account homes are ``AccountRegistry``, sign-in and
probes are ``CliSignIn``, seats and the room are reached through injected
callables so this stays testable without spawning anything.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..agents.cli_accounts import (
    PROVIDER_TITLES,
    PROVIDERS,
    AccountError,
    AccountProfile,
    AccountRegistry,
    Provider,
)
from ..agents.cli_signin import AccountStatus, CliSignIn, LoginSnapshot
from ..core.protocol import (
    ControlAction,
    ControlLogin,
    ControlModelSlot,
    ControlProfile,
    ControlProvider,
    ControlSeat,
    ControlState,
)
from .models import ModelRegistry

log = logging.getLogger(__name__)

PHONE_MODEL_SLOTS = ("codex", "claude", "assistant")


@dataclass(frozen=True)
class BrainstormRoom:
    """What the room looks like right now, for the phone."""

    state: str
    phase: str
    detail: str
    seats: tuple[tuple[str, bool], ...]
    """(seat name, binary installed) in seating order."""


class BrainstormControl:
    """Applies phone-screen actions and publishes the resulting state."""

    def __init__(
        self,
        *,
        registry: AccountRegistry,
        models: ModelRegistry,
        send: Callable[[ControlState], Awaitable[None]],
        room: Callable[[], BrainstormRoom],
        refresh_seats: Callable[[], None],
        reset_room: Callable[[], Awaitable[bool]],
        warm_seats: Callable[[], Awaitable[None]],
        observed: Callable[[], Mapping[str, str]] = dict,
        signin: CliSignIn | None = None,
    ) -> None:
        self._registry = registry
        self._models = models
        self._send = send
        self._room = room
        self._refresh_seats = refresh_seats
        self._reset_room = reset_room
        self._warm_seats = warm_seats
        self._observed = observed
        self._signin = signin if signin is not None else CliSignIn(
            registry, on_change=self._on_login_change, on_signed_in=self._on_signed_in,
        )
        self._status: dict[tuple[str, str], AccountStatus] = {}
        self._revision = 0
        self._notice = ""
        self._publish_task: asyncio.Task[None] | None = None
        self._publish_dirty = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    # ── inbound ─────────────────────────────────────────────────────────────

    def handle(self, action: ControlAction) -> None:
        """Gateway callback: run the action off the socket's reader."""
        self._spawn(self.apply(action))

    async def apply(self, action: ControlAction) -> None:
        async with self._lock:
            try:
                self._notice = await self._apply(action)
            except AccountError as exc:
                self._notice = str(exc)
            except Exception:
                log.exception("control action %s failed", action.action)
                self._notice = "That didn't work on the Mac — try again."
        await self.publish()

    async def _apply(self, action: ControlAction) -> str:
        name = action.action
        if name == "refresh":
            self._refresh_seats()
            await self.probe_all()
            return "Updated."
        if name == "brainstorm.reset":
            cancelled = await self._reset_room()
            self._refresh_seats()
            await self._warm_seats()
            await self.probe_all()
            return ("Brainstorm stopped and reset." if cancelled
                    else "Brainstorm reset — ready for a new question.")
        if name == "account.cancel":
            self._signin.cancel()
            return ""
        if name == "account.dismiss":
            self._signin.dismiss()
            return ""
        if name == "account.code":
            if self._signin.submit_code(action.code or ""):
                return ""
            return "No sign-in is waiting for a code."
        if name == "model.select":
            slot, option = action.slot or "", action.option or ""
            if slot not in PHONE_MODEL_SLOTS:
                return "Unknown model setting."
            return await self._models.select(slot, option)
        provider = action.provider
        if provider is None:
            return "Unknown account type."
        title = PROVIDER_TITLES[provider]
        if name == "account.add":
            profile = self._registry.add(provider, action.label or "")
            self._signin.start(profile)
            return f"Sign in to {title} for “{profile.label}”."
        profile = self._registry.get(provider, action.profile_id or "")
        if name == "account.signin":
            self._signin.start(profile)
            return ""
        if name == "account.activate":
            self._registry.activate(provider, profile.profile_id)
            await self._after_account_change(provider)
            status = self._status.get((provider, profile.profile_id))
            if status is not None and status.state == "logged_out":
                return f"{title} now uses “{profile.label}” — sign it in to use it."
            return f"{title} now uses “{profile.label}”."
        if name == "account.rename":
            renamed = self._registry.rename(provider, profile.profile_id, action.label or "")
            return f"Renamed to “{renamed.label}”."
        if name == "account.signout":
            self._cancel_signin_for(profile)
            ok = await self._signin.sign_out(profile)
            await self._after_account_change(provider)
            return f"Signed out of “{profile.label}”." if ok else "Sign-out didn't finish."
        if name == "account.remove":
            if profile.is_default:
                return "This Mac's own login can be signed out, not removed."
            self._cancel_signin_for(profile)
            if not await self._signin.sign_out(profile):
                # Deleting the home of an account the CLI still holds a login
                # for orphans that login (Claude keeps it in the Keychain),
                # and nothing could revoke it afterwards. Keep the account
                # unless the CLI itself says nobody is signed in.
                status = await self._signin.status(profile)
                if status.state == "logged_in":
                    return (f"Sign-out didn't finish, so “{profile.label}” was kept. "
                            "Try again.")
            removed = self._registry.remove(provider, profile.profile_id)
            self._forget_home(removed)
            self._status.pop((provider, profile.profile_id), None)
            await self._after_account_change(provider)
            return f"Removed “{removed.label}”."
        return "Unknown action."

    # ── outbound ────────────────────────────────────────────────────────────

    def publish_soon(self) -> None:
        """Coalesce bursts (a phase change and an account flip) into one send.

        A change that lands while a send is in flight marks the state dirty
        and is sent after it: the snapshot is taken when a send starts, so
        returning early used to drop it — a device code that arrived while
        the phone was slow to take the previous frame never reached it.
        """
        self._publish_dirty = True
        if self._publish_task is not None and not self._publish_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._publish_task = loop.create_task(self._publish_while_dirty())

    async def _publish_while_dirty(self) -> None:
        while self._publish_dirty:
            self._publish_dirty = False
            await self.publish()

    async def publish(self) -> None:
        self._revision += 1
        try:
            await self._send(self.snapshot())
        except Exception:
            log.warning("control state not sent", exc_info=False)

    def snapshot(self) -> ControlState:
        room = self._room()
        observed = self._observed()
        seats = tuple(
            ControlSeat(
                name=seat,
                title=seat.capitalize(),
                installed=installed,
                account=self._seat_account(seat, installed, observed),
                account_label=self._registry.active(provider).label if provider else "",
                model=self._model_label(seat),
            )
            for seat, installed in room.seats
            for provider in [_provider_of(seat)]
        )
        accounts = tuple(self._provider_view(provider) for provider in PROVIDERS)
        models = tuple(
            ControlModelSlot(
                key=slot.key, title=slot.title, current=slot.current.label,
                options=tuple(option.label for option in slot.options),
            )
            for slot in self._models.slots if slot.key in PHONE_MODEL_SLOTS
        )
        return ControlState(
            revision=self._revision,
            brainstorm_state=room.state,
            brainstorm_phase=room.phase,
            brainstorm_detail=room.detail,
            seats=seats,
            accounts=accounts,
            models=models,
            login=_login_view(self._signin.current),
            notice=self._notice,
        )

    # ── accounts ────────────────────────────────────────────────────────────

    async def probe_all(self) -> None:
        profiles = [p for provider in PROVIDERS for p in self._registry.profiles(provider)]
        results = await asyncio.gather(
            *(self._signin.status(p) for p in profiles), return_exceptions=True,
        )
        for profile, result in zip(profiles, results, strict=True):
            if isinstance(result, AccountStatus):
                self._status[(profile.provider, profile.profile_id)] = result
            else:
                self._status[(profile.provider, profile.profile_id)] = AccountStatus("unknown")

    async def _after_account_change(self, provider: Provider) -> None:
        """A seat's login changed: rebuild seats, re-probe, re-warm."""
        self._refresh_seats()
        await self.probe_all()
        await self._warm_seats()
        log.info("%s seats now run as account %s", provider,
                 self._registry.active(provider).profile_id)

    def _on_login_change(self, _snapshot: LoginSnapshot | None) -> None:
        self.publish_soon()

    def _on_signed_in(self, profile: AccountProfile) -> None:
        """A finished sign-in is the wearer choosing that account."""

        async def adopt() -> None:
            async with self._lock:
                try:
                    self._registry.activate(profile.provider, profile.profile_id)
                except AccountError:
                    # Removed while its sign-in was finishing.
                    self._notice = f"“{profile.label}” was removed before its sign-in finished."
                else:
                    await self._after_account_change(profile.provider)
                    self._notice = (f"{PROVIDER_TITLES[profile.provider]} signed in as "
                                    f"“{profile.label}” and now in use.")
            await self.publish()

        self._spawn(adopt())

    def _provider_view(self, provider: Provider) -> ControlProvider:
        active = self._registry.active(provider)
        installed = self._signin.binary(provider) is not None
        profiles = []
        for profile in self._registry.profiles(provider):
            status = self._status.get((provider, profile.profile_id), AccountStatus("unknown"))
            state = status.state if installed else "missing"
            profiles.append(ControlProfile(
                profile_id=profile.profile_id, label=profile.label, state=state,
                is_default=profile.is_default, active=profile.profile_id == active.profile_id,
                email=status.email, plan=status.plan,
            ))
        return ControlProvider(provider=provider, title=PROVIDER_TITLES[provider],
                               installed=installed, profiles=tuple(profiles))

    def _seat_account(self, seat: str, installed: bool, observed: Mapping[str, str]) -> str:
        if not installed:
            return "missing"
        provider = _provider_of(seat)
        if provider is None:
            return "unknown"
        live = observed.get(seat)
        if live in ("limited", "logged_out"):
            return live
        active = self._registry.active(provider)
        return self._status.get((provider, active.profile_id), AccountStatus("unknown")).state

    def _model_label(self, seat: str) -> str:
        slot = self._models.slot(seat)
        return slot.current.label if slot is not None else ""

    def _forget_home(self, profile: AccountProfile) -> None:
        """Delete a removed account's home — only one JOAT created itself."""
        if profile.home is None:
            return
        home = Path(profile.home)
        if home.name == profile.profile_id and home.parent.name == profile.provider:
            shutil.rmtree(home, ignore_errors=True)

    def _cancel_signin_for(self, profile: AccountProfile) -> None:
        """Stop a sign-in still running for an account being signed out or
        removed: left alone, the CLI kept writing into a home that was about
        to be deleted, and finishing it re-activated a removed account."""
        current = self._signin.current
        if (current is not None and current.provider == profile.provider
                and current.profile_id == profile.profile_id
                and current.stage not in ("done", "failed", "cancelled")):
            self._signin.cancel()

    def _spawn(self, coro: Awaitable[None]) -> None:
        async def run() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("phone control task failed")

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        self._signin.cancel(publish=False)
        for task in list(self._tasks):
            task.cancel()


def _provider_of(seat: str) -> Provider | None:
    if seat == "claude":
        return "claude"
    if seat == "codex":
        return "codex"
    return None


def _login_view(snapshot: LoginSnapshot | None) -> ControlLogin | None:
    if snapshot is None:
        return None
    return ControlLogin(
        provider=snapshot.provider, profile_id=snapshot.profile_id, stage=snapshot.stage,
        message=snapshot.message, url=snapshot.url, code=snapshot.code,
        needs_code=snapshot.needs_code,
    )


__all__ = ["BrainstormControl", "BrainstormRoom", "PHONE_MODEL_SLOTS"]
