"""PairingStore: the token that separates the wearer's phone from the LAN."""

from __future__ import annotations

import json
import stat
from pathlib import Path

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
