"""Explicit, time-bounded pairing for the hub gateway.

The gateway accepts connections from a network peer, which can write to the
HUD, stream mic-shaped input, and answer confirmation gates. A missing or
corrupt token file therefore must not silently authorize the next LAN peer.
Pairing is permitted only during a short window explicitly opened on the Mac.

Two stores hold the key:

- ``ShellPairingStore`` when JOAT.app runs the engine. The key lives in the
  login keychain, readable without a system prompt only by JOAT.app, which
  hands it to the engine over the private inherited pipe. No file holds it,
  and only the app's Pair Glasses opens a window (2026-10-13: any program
  running as the wearer could read pairing.json, or write the window marker).
- ``PairingStore``, the file, when the engine is run by hand.

To pair or replace a phone, use the native Mac app's Pair Glasses action.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

_DEFAULT_PATH = "~/Library/Application Support/JOAT/pairing.json"
_WINDOW_FILENAME = "pairing-window.json"
_DEFAULT_WINDOW_SECONDS = 120.0
_MAX_WINDOW_SECONDS = 300.0
#: The plugin keeps any non-empty token up to this length (Pairing.ts).
_MAX_TOKEN_LENGTH = 4096


def is_pairing_token(value: object) -> bool:
    """A token the plugin could hold: a bounded, non-empty string."""
    return isinstance(value, str) and 0 < len(value) <= _MAX_TOKEN_LENGTH and "\x00" not in value


class PairingAuthority(Protocol):
    """What the gateway needs from whichever store holds the key."""

    @property
    def token(self) -> str | None: ...

    @property
    def window_open(self) -> bool: ...

    def begin_pairing(self, duration_s: float = _DEFAULT_WINDOW_SECONDS) -> None: ...

    def forget(self) -> bool: ...

    def confirm(self, token: str) -> None: ...


class PairingStore:
    """The persisted pairing token — one per engine install."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path or Path(os.path.expanduser(_DEFAULT_PATH))
        self._window_path = self._path.with_name(_WINDOW_FILENAME)
        self._clock = clock
        self._token: str | None = None
        self._store_error_logged = False
        self._window_error_logged = False

    def _read_token(self) -> str | None:
        try:
            data = json.loads(self._path.read_text())
            token = data.get("token")
            if isinstance(token, str) and token:
                self._store_error_logged = False
                return token
        except FileNotFoundError:
            self._store_error_logged = False
            return None
        except (OSError, json.JSONDecodeError):
            # Corruption is not consent to trust the next network peer.
            if not self._store_error_logged:
                log.error("pairing store unreadable — pairing remains closed")
                self._store_error_logged = True
        return None

    @property
    def token(self) -> str | None:
        """The current token, re-checked against the file on every read.

        A deleted or unreadable file clears the in-memory value but does not
        open pairing. Only ``begin_pairing`` creates that authorization.
        """
        self._token = self._read_token()
        return self._token

    @property
    def window_path(self) -> Path:
        """The local-authority marker shared with the native shell."""
        return self._window_path

    @property
    def window_open(self) -> bool:
        """Whether an explicit, owner-only, unexpired pairing window exists."""
        if self.token is not None:
            return False
        try:
            mode = stat.S_IMODE(self._window_path.stat().st_mode)
            if mode & 0o077:
                if not self._window_error_logged:
                    log.error("pairing window has unsafe permissions — refusing it")
                    self._window_error_logged = True
                return False
            data = json.loads(self._window_path.read_text())
            expires_at = float(data.get("expiresAt", 0))
            self._window_error_logged = False
        except FileNotFoundError:
            self._window_error_logged = False
            return False
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            if not self._window_error_logged:
                log.error("pairing window unreadable — pairing remains closed")
                self._window_error_logged = True
            return False
        if expires_at <= self._clock():
            try:
                self._window_path.unlink()
            except OSError:
                pass
            return False
        return True

    def begin_pairing(self, duration_s: float = _DEFAULT_WINDOW_SECONDS) -> None:
        """Open a bounded window after an explicit local user action.

        The marker is committed before the old token is removed. If removing
        the token fails, the marker is rolled back so a partial filesystem
        operation cannot broaden trust.
        """
        if not 0 < duration_s <= _MAX_WINDOW_SECONDS:
            raise ValueError(
                f"pairing window must be between 0 and {_MAX_WINDOW_SECONDS:g} seconds"
            )
        self._window_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._window_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"expiresAt": self._clock() + duration_s}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._window_path)
        self._window_error_logged = False
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            try:
                self._window_path.unlink()
            except OSError:
                pass
            raise
        self._token = None

    def forget(self) -> bool:
        """Compatibility wrapper: forget the token and open a local window."""
        had_token = self.token is not None
        self.begin_pairing()
        return had_token

    @staticmethod
    def mint() -> str:
        """A fresh candidate token — NOT yet trusted or persisted.

        Minting and confirming are separate because persisting at issue time
        strands the wearer: if the pairing frame is lost (Wi-Fi blip at
        QR-scan time), the Mac would hold a token no phone has, and 401 the
        real phone forever. The token becomes real only once the plugin acks
        that it stored it.
        """
        return secrets.token_urlsafe(24)

    def confirm(self, token: str) -> None:
        """Persist a token the plugin has acknowledged storing."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"token": token}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        self._token = token
        self._store_error_logged = False
        try:
            self._window_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # A valid token still keeps the gateway closed to untrusted peers;
            # the marker will also expire independently.
            log.warning("paired successfully but could not remove expired window marker")


class ShellPairingStore:
    """The pairing key as JOAT.app holds it: in the keychain, never a file.

    JOAT.app reads the key from the login keychain (an item only the app can
    read without macOS asking the wearer) and sends it, with any pairing
    window its Pair Glasses opened, over the private pipe it launched the
    engine with (services/local_commands.py). A phone paired during that
    window is handed back the same way, to be saved in the keychain.

    Until the app has said what it holds, nothing is paired and no window is
    open, so every peer is refused: the safe direction.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._token: str | None = None
        self._window_until: float | None = None
        self._on_confirmed: Callable[[str], None] | None = None
        self._known = asyncio.Event()

    @property
    def known(self) -> bool:
        """Whether JOAT.app has sent what it holds yet."""
        return self._known.is_set()

    async def wait_known(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self._known.wait(), timeout_s)
        except TimeoutError:
            return False
        return True

    def set_on_confirmed(self, callback: Callable[[str], None] | None) -> None:
        """Where a newly paired key goes: the pipe back to JOAT.app."""
        self._on_confirmed = callback

    def apply(self, token: str | None, window_until: float | None) -> None:
        """Adopt JOAT.app's state: its key, or no key and maybe a window."""
        if token is not None and not is_pairing_token(token):
            raise ValueError("invalid pairing token")
        if window_until is not None and not math.isfinite(window_until):
            raise ValueError("invalid pairing window")
        now = self._clock()
        self._token = token
        if token is None and window_until is not None and window_until > now:
            self._window_until = min(window_until, now + _MAX_WINDOW_SECONDS)
        else:
            self._window_until = None
        self._known.set()

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def window_open(self) -> bool:
        return (
            self._token is None
            and self._window_until is not None
            and self._window_until > self._clock()
        )

    def begin_pairing(self, duration_s: float = _DEFAULT_WINDOW_SECONDS) -> None:
        """Refused: only JOAT.app's Pair Glasses opens a window here."""
        raise PermissionError("pairing windows are opened from JOAT.app")

    def forget(self) -> bool:
        self.begin_pairing()
        return False

    def confirm(self, token: str) -> None:
        """A phone acknowledged this key: use it, and hand it to JOAT.app."""
        self._token = token
        self._window_until = None
        callback = self._on_confirmed
        if callback is None:
            log.error("paired, but JOAT.app is not connected to keep the key — "
                      "the phone must pair again after the next restart")
            return
        try:
            callback(token)
        except Exception:
            log.error("paired, but the key could not be handed to JOAT.app — "
                      "the phone must pair again after the next restart")
