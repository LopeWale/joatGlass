"""Sign a saved account in or out with the official CLI, finished on the phone.

Both sign-ins were run on this Mac before this was written (2026-10-05) in a
pseudo-terminal with throwaway homes, and they print what a phone can
finish without the Mac's browser:

- ``codex login --device-auth`` (Codex 0.160.0): a link,
  ``https://auth.openai.com/codex/device``, and a one-time code such as
  ``GBEA-VS5GL`` that "expires in 15 minutes". The CLI exits 0 once the
  code is accepted in any browser. Personal accounts must allow device
  code sign-in in ChatGPT → Settings → Security first.
- ``claude auth login --claudeai`` (Claude Code 2.1.287): "If the browser
  didn't open, visit: <authorize URL>" whose redirect is
  ``platform.claude.com/oauth/code/callback`` — the page that SHOWS a code —
  then ``Paste code here if prompted >``. The docs: "If your browser shows
  a login code instead of redirecting back after you sign in, paste it into
  the terminal at the Paste code here if prompted prompt."

The pty is required: both CLIs print their prompts only to a terminal, and
Claude reads the pasted code from one. Nothing printed here is logged — the
link carries a PKCE state and the code is a one-time credential.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import select
import signal
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from .cli_accounts import AMBIENT_LOGIN_ENV, AccountProfile, AccountRegistry, Provider
from .cli_env import cli_env
from .cli_locate import find_codex_binary
from .subprocess_agent import find_claude_binary

log = logging.getLogger(__name__)

LoginStage = Literal[
    "starting", "open_link", "enter_code", "completing", "done", "failed", "cancelled",
]
AccountState = Literal["logged_in", "logged_out", "missing", "unknown"]

SIGN_IN_TIMEOUT_S = 15 * 60.0
"""Codex's own device code lifetime; Claude's flow gets the same window."""
CODE_CHECK_TIMEOUT_S = 90.0
"""How long "Checking the code…" may last before the phone says so."""
PROBE_TIMEOUT_S = 20.0
MAX_PASTED_CODE = 512
SIGN_IN_STRIP_ENV = tuple(key for keys in AMBIENT_LOGIN_ENV.values() for key in keys)
"""Dropped on top of the shared list (`cli_env.STRIP_ENV`): a sign-in must
create a subscription login, never be short-circuited by one the brain
inherited, the Mac's own login token included."""

CODEX_DEVICE_HINT = (
    "If Continue is greyed out, allow device code sign-in in ChatGPT → "
    "Settings → Security, then try again."
)

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_URL = re.compile(r"https://[^\s\x1b\x07]+")
_DEVICE_CODE = re.compile(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,6})\b")


def strip_terminal(text: str) -> str:
    """Printable text from a terminal stream: no colour, no hyperlink frames."""
    return _CSI.sub("", _OSC.sub("", text)).replace("\r", "")


@dataclass(frozen=True)
class LoginSnapshot:
    """What the phone shows for an in-progress sign-in."""

    provider: Provider
    profile_id: str
    stage: LoginStage
    url: str | None = None
    code: str | None = None
    """Codex only: the one-time code the wearer types on the website."""
    needs_code: bool = False
    """Claude only: the website shows a code the wearer pastes back here."""
    message: str = ""


@dataclass(frozen=True)
class AccountStatus:
    state: AccountState
    email: str | None = None
    plan: str | None = None


def parse_claude_status(output: str) -> AccountStatus:
    """``claude auth status`` prints one JSON object (2.1.287)."""
    start = output.find("{")
    if start == -1:
        return AccountStatus("unknown")
    try:
        payload, _ = json.JSONDecoder().raw_decode(output[start:])
    except ValueError:
        return AccountStatus("unknown")
    if not isinstance(payload, dict):
        return AccountStatus("unknown")
    if not payload.get("loggedIn"):
        return AccountStatus("logged_out")
    email = payload.get("email") or payload.get("emailAddress")
    plan = payload.get("subscriptionType") or payload.get("plan")
    return AccountStatus(
        "logged_in",
        email=email if isinstance(email, str) and email else None,
        plan=plan if isinstance(plan, str) and plan else None,
    )


def parse_codex_status(output: str) -> AccountStatus:
    """``codex login status``: "Logged in using ChatGPT" (0.160.0)."""
    lowered = output.lower()
    if "not logged in" in lowered:
        return AccountStatus("logged_out")
    if "logged in" in lowered:
        plan = "ChatGPT" if "chatgpt" in lowered else ("API key" if "api key" in lowered else None)
        return AccountStatus("logged_in", plan=plan)
    return AccountStatus("unknown")


class CliSignIn:
    """Probe, sign in and sign out saved accounts; one sign-in at a time."""

    def __init__(
        self,
        registry: AccountRegistry,
        *,
        on_change: Callable[[LoginSnapshot | None], None] | None = None,
        on_signed_in: Callable[[AccountProfile], None] | None = None,
        claude_binary: Callable[[], str | None] = find_claude_binary,
        codex_binary: Callable[[], str | None] = find_codex_binary,
        timeout_s: float = SIGN_IN_TIMEOUT_S,
    ) -> None:
        self._registry = registry
        self._on_change = on_change
        self._on_signed_in = on_signed_in
        self._binaries: dict[Provider, Callable[[], str | None]] = {
            "claude": claude_binary, "codex": codex_binary,
        }
        self._timeout_s = timeout_s
        self._code_check_timeout_s = min(CODE_CHECK_TIMEOUT_S, timeout_s)
        self._snapshot: LoginSnapshot | None = None
        # Characters read from the CLI so far, and the count when a code was
        # pasted: what the CLI printed after the paste is its answer to it.
        self._received = 0
        self._code_mark: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def current(self) -> LoginSnapshot | None:
        return self._snapshot

    def binary(self, provider: Provider) -> str | None:
        return self._binaries[provider]()

    # ── probes ──────────────────────────────────────────────────────────────

    def _env(self, profile: AccountProfile) -> dict[str, str]:
        env = cli_env(SIGN_IN_STRIP_ENV)
        for key, value in self._registry.env_overlay(profile.provider, profile).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    async def _run(self, argv: list[str], profile: AccountProfile) -> tuple[int | None, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=self._env(profile),
            )
        except OSError as exc:
            log.warning("%s probe could not start (%s)", profile.provider, type(exc).__name__)
            return 127, ""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return None, ""
        return proc.returncode, out.decode(errors="replace")

    async def status(self, profile: AccountProfile) -> AccountStatus:
        binary = self.binary(profile.provider)
        if binary is None:
            return AccountStatus("missing")
        if profile.provider == "claude":
            _code, out = await self._run([binary, "auth", "status"], profile)
            return parse_claude_status(out)
        _code, out = await self._run([binary, "login", "status"], profile)
        return parse_codex_status(out)

    async def sign_out(self, profile: AccountProfile) -> bool:
        """Revoke and clear this account's login through the CLI itself."""
        binary = self.binary(profile.provider)
        if binary is None:
            return False
        argv = [binary, "auth", "logout"] if profile.provider == "claude" else [binary, "logout"]
        code, _ = await self._run(argv, profile)
        log.info("%s account %s signed out (rc=%s)", profile.provider, profile.profile_id, code)
        return code == 0

    # ── the sign-in flow ────────────────────────────────────────────────────

    def start(self, profile: AccountProfile) -> LoginSnapshot:
        """Begin a sign-in for this account, replacing any other in progress."""
        self.cancel(publish=False)
        self._set(LoginSnapshot(profile.provider, profile.profile_id, "starting",
                                message="Starting sign-in on the Mac…"))
        self._task = asyncio.get_running_loop().create_task(
            self._run_flow(profile), name=f"signin-{profile.provider}",
        )
        assert self._snapshot is not None
        return self._snapshot

    def submit_code(self, code: str) -> bool:
        """Hand the code the Claude website showed to the waiting CLI."""
        snapshot = self._snapshot
        cleaned = "".join(str(code).split())
        if (snapshot is None or not snapshot.needs_code or self._master_fd is None
                or not cleaned or len(cleaned) > MAX_PASTED_CODE or not cleaned.isprintable()):
            return False
        try:
            os.write(self._master_fd, cleaned.encode() + b"\r")
        except OSError:
            return False
        self._code_mark = self._received
        self._set(replace(snapshot, stage="completing", needs_code=False,
                          message="Checking the code…"))
        return True

    def cancel(self, *, publish: bool = True) -> None:
        task, self._task = self._task, None
        self._kill()
        if task is not None and not task.done():
            task.cancel()
        snapshot = self._snapshot
        if publish and snapshot is not None and snapshot.stage not in ("done", "failed", "cancelled"):
            self._set(replace(snapshot, stage="cancelled", url=None, code=None,
                              needs_code=False, message="Sign-in cancelled."))

    def dismiss(self) -> None:
        """Clear a finished sign-in from the phone."""
        if self._snapshot is not None and self._snapshot.stage in ("done", "failed", "cancelled"):
            self._set(None)

    def _reprompted(self, snapshot: LoginSnapshot, text: str) -> bool:
        """The CLI asked for a code again after one was pasted."""
        if snapshot.stage != "completing" or self._code_mark is None:
            return False
        since = self._received - self._code_mark
        if since <= 0:
            return False
        return "paste code" in text[-since:].lower()

    def _set(self, snapshot: LoginSnapshot | None) -> None:
        self._snapshot = snapshot
        if self._on_change is not None:
            try:
                self._on_change(snapshot)
            except Exception:
                log.warning("sign-in observer failed", exc_info=False)

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        fd, self._master_fd = self._master_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    async def _run_flow(self, profile: AccountProfile) -> None:
        provider = profile.provider
        binary = self.binary(provider)
        base = LoginSnapshot(provider, profile.profile_id, "starting")
        if binary is None:
            self._set(replace(base, stage="failed",
                              message=f"{provider.capitalize()} isn't installed on the Mac."))
            return
        argv = ([binary, "auth", "login", "--claudeai"] if provider == "claude"
                else [binary, "login", "--device-auth"])
        master, slave = os.openpty()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=slave, stdout=slave, stderr=slave,
                env=self._env(profile), start_new_session=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(slave)
            log.warning("%s sign-in could not start (%s)", provider, type(exc).__name__)
            self._set(replace(base, stage="failed", message="Sign-in could not start on the Mac."))
            return
        os.close(slave)
        self._proc = proc
        self._master_fd = master
        log.info("%s sign-in started for account %s", provider, profile.profile_id)
        text = ""
        self._received = 0
        self._code_mark = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s
        sent_enter = False
        checking_since: float | None = None
        try:
            while loop.time() < deadline:
                chunk = await asyncio.to_thread(_read_some, master, 0.5)
                if chunk is None:
                    break
                checking = (self._snapshot or base).stage == "completing"
                if not checking:
                    checking_since = None
                elif checking_since is None:
                    checking_since = loop.time()
                elif loop.time() - checking_since > self._code_check_timeout_s:
                    # The CLI neither accepted the code nor asked again.
                    self._kill()
                    self._set(replace(self._snapshot or base, stage="failed", url=None,
                                      code=None, needs_code=False,
                                      message="The code wasn't accepted. Start the sign-in again."))
                    return
                if not chunk:
                    if proc.returncode is not None:
                        break
                    continue
                fresh = strip_terminal(chunk.decode(errors="replace"))
                self._received += len(fresh)
                text = (text + fresh)[-20_000:]
                snapshot = self._snapshot or base
                if self._reprompted(snapshot, text):
                    # A wrong or mistyped code: Claude says so and asks again.
                    # The phone used to stay on "Checking the code…" until
                    # the 15-minute deadline.
                    self._code_mark = None
                    self._set(replace(snapshot, stage="enter_code", needs_code=True,
                                      message="That code didn't work. Copy it again and paste it."))
                    continue
                updated = _advance(snapshot, text)
                if updated != snapshot:
                    self._set(updated)
                if (not sent_enter and "login successful" in text.lower()
                        and "enter" in text.lower()):
                    sent_enter = True
                    os.write(master, b"\r")
            else:
                self._kill()
                self._set(replace(self._snapshot or base, stage="failed", url=None, code=None,
                                  needs_code=False,
                                  message="The sign-in link expired. Start again."))
                return
            try:
                await asyncio.wait_for(proc.wait(), 10.0)
            except TimeoutError:
                self._kill()
            ok = proc.returncode == 0
            if ok:
                log.info("%s sign-in finished for account %s", provider, profile.profile_id)
                self._set(replace(self._snapshot or base, stage="done", url=None, code=None,
                                  needs_code=False, message="Signed in."))
                if self._on_signed_in is not None:
                    try:
                        self._on_signed_in(profile)
                    except Exception:
                        log.exception("sign-in observer failed")
            else:
                log.info("%s sign-in ended rc=%s", provider, proc.returncode)
                hint = f" {CODEX_DEVICE_HINT}" if provider == "codex" else ""
                self._set(replace(self._snapshot or base, stage="failed", url=None, code=None,
                                  needs_code=False,
                                  message=f"Sign-in did not finish.{hint}"))
        except asyncio.CancelledError:
            self._kill()
            raise
        finally:
            if self._master_fd == master:
                self._kill()


def _read_some(fd: int, timeout_s: float) -> bytes | None:
    """b'' on a quiet interval, None at end of stream (the child exited)."""
    try:
        ready, _, _ = select.select([fd], [], [], timeout_s)
    except (OSError, ValueError):
        return None
    if not ready:
        return b""
    try:
        data = os.read(fd, 4096)
    except OSError:
        return None  # EIO: the pty's other end is gone
    return data if data else None


def _advance(snapshot: LoginSnapshot, text: str) -> LoginSnapshot:
    """Move the phone's view forward from what the CLI has printed so far."""
    # enter_code: everything the phone needs is on it until a code is
    # pasted, and re-deriving it here would overwrite a "didn't work" note.
    if snapshot.stage in ("enter_code", "completing", "done", "failed", "cancelled"):
        return snapshot
    urls = _URL.findall(text)
    if snapshot.provider == "claude":
        url = next((u for u in urls if "oauth/authorize" in u), urls[0] if urls else None)
        if url and "paste code" in text.lower():
            return replace(snapshot, stage="enter_code", url=url, needs_code=True,
                           message="Open the link, sign in, then paste the code it shows.")
        if url:
            return replace(snapshot, stage="open_link", url=url,
                           message="Open the link and sign in.")
        return snapshot
    url = next((u for u in urls if "auth.openai.com" in u), urls[0] if urls else None)
    tail = text.split("one-time code", 1)[1] if "one-time code" in text else ""
    found = _DEVICE_CODE.search(tail)
    if url and found:
        return replace(snapshot, stage="open_link", url=url, code=found.group(1),
                       message=f"Open the link, sign in, and enter the code. {CODEX_DEVICE_HINT}")
    return snapshot


__all__ = [
    "CODEX_DEVICE_HINT",
    "AccountState",
    "AccountStatus",
    "CliSignIn",
    "LoginSnapshot",
    "LoginStage",
    "parse_claude_status",
    "parse_codex_status",
    "strip_terminal",
]
