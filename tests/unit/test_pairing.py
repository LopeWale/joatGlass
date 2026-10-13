"""PairingStore: the token that separates the wearer's phone from the LAN."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

from joat.ble.hub_gateway import _RedactToken
from joat.core.pairing import PairingStore


class TestPairingStore:
    def test_fresh_store_has_no_token(self, tmp_path: Path) -> None:
        store = PairingStore(tmp_path / "pairing.json")
        assert store.token is None
        assert store.window_open is False

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

    def test_corrupt_file_fails_closed_without_reopening_pairing(self, tmp_path: Path) -> None:
        path = tmp_path / "pairing.json"
        path.write_text("not json{")
        store = PairingStore(path)
        assert store.token is None
        assert store.window_open is False

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

    def test_delete_file_does_not_reopen_pairing(self, tmp_path: Path) -> None:
        # Filesystem loss is not physical-user consent to trust the next LAN peer.
        path = tmp_path / "pairing.json"
        store = PairingStore(path)
        store.confirm(PairingStore.mint())
        path.unlink()
        assert PairingStore(path).token is None
        assert store.window_open is False

    def test_explicit_window_is_time_bounded_and_owner_only(self, tmp_path: Path) -> None:
        now = [1_000.0]
        store = PairingStore(tmp_path / "pairing.json", clock=lambda: now[0])

        store.begin_pairing(duration_s=30)

        assert store.window_open is True
        assert stat.S_IMODE(store.window_path.stat().st_mode) == 0o600
        now[0] = 1_031.0
        assert store.window_open is False

    def test_confirming_pairing_consumes_the_window(self, tmp_path: Path) -> None:
        store = PairingStore(tmp_path / "pairing.json")
        store.begin_pairing()
        assert store.window_open is True

        store.confirm(PairingStore.mint())

        assert store.window_open is False


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

    def test_one_time_confirmation_challenge_is_redacted(self) -> None:
        rec = self._record('{"kind":"confirm.request","challengeId":"one-time-secret"}')
        _RedactToken().filter(rec)
        assert "one-time-secret" not in rec.getMessage()
        assert '"challengeId":"<redacted>"' in rec.getMessage()

    def test_other_query_params_survive(self) -> None:
        rec = self._record("< %s %s", "GET", "/glasses?a=1&token=s3cr3t&b=2")
        _RedactToken().filter(rec)
        out = rec.getMessage()
        assert "a=1" in out and "b=2" in out and "s3cr3t" not in out

    def test_records_without_a_token_are_untouched(self) -> None:
        rec = self._record("< %s %s", "GET", "/glasses")
        _RedactToken().filter(rec)
        assert rec.getMessage() == "< GET /glasses"

    def test_navigation_frame_content_and_precise_geometry_are_redacted(self) -> None:
        class FrameLike:
            data = (
                '{"kind":"navigation.route","sessionId":"nav-1",'
                '"destinationLabel":"Private Clinic","etaMinutes":4,'
                '"plan":{"geometry":[{"latitude":41.9,"longitude":-87.6}],'
                '"steps":[{"instruction":"Turn onto Private Street"}]}}'
            )

            def __str__(self) -> str:
                return self.data

        rec = self._record("< %s", FrameLike())
        _RedactToken().filter(rec)
        rendered = rec.getMessage()
        assert "Private Clinic" not in rendered
        assert "Private Street" not in rendered
        assert "41.9" not in rendered
        assert '"plan": "<redacted>"' in rendered


class TestRecovery:
    """Recovery requires an explicit local action and no engine restart."""

    def test_deleting_the_file_keeps_the_window_closed(self, tmp_path: Path) -> None:
        path = tmp_path / "pairing.json"
        store = PairingStore(path)
        store.confirm(PairingStore.mint())
        assert store.token is not None
        path.unlink()
        assert store.token is None
        assert store.window_open is False

    def test_begin_pairing_reopens_the_window(self, tmp_path: Path) -> None:
        path = tmp_path / "pairing.json"
        store = PairingStore(path)
        store.confirm(PairingStore.mint())

        store.begin_pairing()

        assert store.token is None
        assert store.window_open is True

    def test_a_present_file_still_enforces_its_token(self, tmp_path: Path) -> None:
        path = tmp_path / "pairing.json"
        store = PairingStore(path)
        token = PairingStore.mint()
        store.confirm(token)
        assert store.token == token
        assert store.token == token  # repeated reads stay stable


class TestShellPairingStore:
    """Under JOAT.app the key lives in its keychain, not in pairing.json."""

    def test_nothing_is_paired_or_open_until_the_app_speaks(self) -> None:
        from joat.core.pairing import ShellPairingStore

        store = ShellPairingStore()
        assert not store.known
        assert store.token is None
        assert store.window_open is False

    def test_the_apps_key_is_the_key(self) -> None:
        from joat.core.pairing import ShellPairingStore

        store = ShellPairingStore()
        store.apply("key-from-keychain", None)
        assert store.known and store.token == "key-from-keychain"
        assert store.window_open is False

    def test_a_window_only_while_unpaired_and_never_past_five_minutes(self) -> None:
        from joat.core.pairing import ShellPairingStore

        now = [1_000.0]
        store = ShellPairingStore(clock=lambda: now[0])
        store.apply(None, now[0] + 3_600)
        assert store.window_open
        now[0] += 301
        assert store.window_open is False
        store.apply("key", now[0] + 60)
        assert store.window_open is False

    def test_only_the_app_opens_a_window(self) -> None:
        import pytest

        from joat.core.pairing import ShellPairingStore

        store = ShellPairingStore()
        with pytest.raises(PermissionError):
            store.begin_pairing()
        assert store.window_open is False

    def test_a_new_phone_key_goes_to_the_app(self) -> None:
        from joat.core.pairing import ShellPairingStore

        handed: list[str] = []
        store = ShellPairingStore()
        store.apply(None, 10**12)
        store.set_on_confirmed(handed.append)
        store.confirm("new-phone-key")
        assert handed == ["new-phone-key"]
        assert store.token == "new-phone-key" and store.window_open is False

    def test_invalid_state_is_refused(self) -> None:
        import math

        import pytest

        from joat.core.pairing import ShellPairingStore

        store = ShellPairingStore()
        with pytest.raises(ValueError):
            store.apply("", None)
        with pytest.raises(ValueError):
            store.apply(None, math.inf)
        assert not store.known
