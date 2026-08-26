"""Trust-on-first-use pairing for the hub gateway.

The gateway must accept connections from the LAN (the phone has to reach
it), and until this module existed it accepted them from ANYONE on the LAN:
write to the HUD, stream fake mic audio into STT, answer the wearer's
confirmation gate. The fix is a pairing token: the first REMOTE client to
connect — the wearer's phone, at QR-scan time — is issued a token, and every
later remote connection must present it or is rejected before the WebSocket
handshake completes.

Loopback clients are exempt by default: the simulator and speak_test run on
the Mac itself, and any local process could read the token file anyway. The
trust boundary this enforces is the network, not the machine.

To pair a different phone: delete the store file and reconnect.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_PATH = "~/Library/Application Support/JOAT/pairing.json"


class PairingStore:
    """The persisted pairing token — one per engine install."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or Path(os.path.expanduser(_DEFAULT_PATH))
        self._token: str | None = None
        try:
            data = json.loads(self._path.read_text())
            token = data.get("token")
            if isinstance(token, str) and token:
                self._token = token
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            # Unreadable is indistinguishable from absent for our purposes:
            # the next remote connect simply re-pairs.
            log.warning("pairing store unreadable — will re-pair on next connect")

    @property
    def token(self) -> str | None:
        return self._token

    def issue(self) -> str:
        """Generate, persist, and return a fresh token (first pairing)."""
        token = secrets.token_urlsafe(24)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"token": token}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        self._token = token
        return token
