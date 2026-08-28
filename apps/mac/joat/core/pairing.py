"""Explicit, time-bounded pairing for the hub gateway.

The gateway accepts connections from a network peer, which can write to the
HUD, stream mic-shaped input, and answer confirmation gates. A missing or
corrupt token file therefore must not silently authorize the next LAN peer.
Pairing is permitted only during a short window explicitly opened on the Mac.

Loopback clients are exempt by default: the simulator and speak_test run on
the Mac itself, and any local process could read the token file anyway. The
trust boundary this enforces is the network, not the machine.

To pair or replace a phone, use the native Mac app's Pair Glasses action.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_PATH = "~/Library/Application Support/JOAT/pairing.json"
_WINDOW_FILENAME = "pairing-window.json"
_DEFAULT_WINDOW_SECONDS = 120.0
_MAX_WINDOW_SECONDS = 300.0


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
