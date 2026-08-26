"""PairingStore: the token that separates the wearer's phone from the LAN."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

from jarvis.ble.hub_gateway import _RedactToken
from jarvis.core.pairing import PairingStore


class TestPairingStore:
    def test_fresh_store_has_no_token(self, tmp_path: Path) -> None:
        assert PairingStore(tmp_path / "pairing.json").token is None

    def test_mint_does_not_persist(self, tmp_path: Path) -> None:
        # A minted-but-unconfirmed token must leave the store unpaired, or a
        # lost pairing frame strands the wearer's phone behind a 401 forever.
        path = tmp_path / "pairing.json"
        token = PairingStore.mint()
        assert token
        assert not path.exists()
        assert PairingStore(path).token is None

    def test_confirm_persists_across_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "pairing.json"
        store = PairingStore(path)
        token = PairingStore.mint()
        store.confirm(token)
        assert store.token == token
        assert PairingStore(path).token == token

    def test_minted_tokens_are_unique(self) -> None:
        assert PairingStore.mint() != PairingStore.mint()

    def test_issued_file_is_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "pairing.json"
        store = PairingStore(path)
        store.confirm(PairingStore.mint())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_corrupt_file_means_unpaired_not_crashed(self, tmp_path: Path) -> None:
        path = tmp_path / "pairing.json"
        path.write_text("not json{")
        assert PairingStore(path).token is None

    def test_blank_token_in_file_means_unpaired(self, tmp_path: Path) -> None:
        # An empty token would make compare_digest("") a universal key.
        path = tmp_path / "pairing.json"
        path.write_text(json.dumps({"token": ""}))
        assert PairingStore(path).token is None

    def test_missing_parent_directory_is_created(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "pairing.json"
        store = PairingStore(path)
        token = PairingStore.mint()
        store.confirm(token)
        assert json.loads(path.read_text())["token"] == token

    def test_delete_file_reopens_pairing(self, tmp_path: Path) -> None:
        # The documented re-pair procedure: rm the file, reconnect.
        path = tmp_path / "pairing.json"
        store = PairingStore(path)
        store.confirm(PairingStore.mint())
        path.unlink()
        assert PairingStore(path).token is None


class TestTokenRedaction:
    """`--verbose` must not write the pairing token into brain.log.

    websockets logs the full request line at DEBUG, and our auth rides the
    query string — so the exact flag an operator enables to debug a pairing
    failure would durably record the secret in a file the app shows in its UI.
    """

    def _record(self, msg: str, *args: object) -> logging.LogRecord:
        return logging.LogRecord("websockets.server", logging.DEBUG, __file__, 1,
                                 msg, args, None)

    def test_token_in_request_line_args_is_redacted(self) -> None:
        rec = self._record("< %s %s %s", "GET", "/glasses?token=s3cr3t-value", "HTTP/1.1")
        _RedactToken().filter(rec)
        assert "s3cr3t-value" not in rec.getMessage()
        assert "token=<redacted>" in rec.getMessage()

    def test_token_in_message_body_is_redacted(self) -> None:
        rec = self._record("GET /glasses?token=s3cr3t-value HTTP/1.1")
        _RedactToken().filter(rec)
        assert "s3cr3t-value" not in rec.getMessage()

    def test_other_query_params_survive(self) -> None:
        rec = self._record("< %s %s", "GET", "/glasses?a=1&token=s3cr3t&b=2")
        _RedactToken().filter(rec)
        out = rec.getMessage()
        assert "a=1" in out and "b=2" in out and "s3cr3t" not in out

    def test_records_without_a_token_are_untouched(self) -> None:
        rec = self._record("< %s %s", "GET", "/glasses")
        _RedactToken().filter(rec)
        assert rec.getMessage() == "< GET /glasses"
