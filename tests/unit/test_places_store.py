"""The saved places are replaced whole and survive a restart."""

from __future__ import annotations

from pathlib import Path

import pytest

from joat.core.protocol import SavedPlace
from joat.persistence.places import PlaceStore
from joat.persistence.store import DurableStore


def test_places_replace_whole_in_order_and_reload(tmp_path: Path) -> None:
    store = DurableStore(tmp_path / "joat.sqlite3")
    try:
        places = PlaceStore(store)
        assert places.list() == []
        home = SavedPlace(place_id="p1", name="Home", address="12 Oak St")
        work = SavedPlace(place_id="p2", name="Work", latitude=35.2, longitude=-97.4)
        places.replace_all([work, home])
        assert places.list() == [work, home]
        places.replace_all([home])
        assert places.list() == [home]
        with pytest.raises(ValueError):
            places.replace_all([home] * 51)
    finally:
        store.close()
    reopened = DurableStore(tmp_path / "joat.sqlite3")
    try:
        assert PlaceStore(reopened).list() == [home]
    finally:
        reopened.close()


def test_the_table_refuses_a_place_with_neither_address_nor_pin(tmp_path: Path) -> None:
    store = DurableStore(tmp_path / "joat.sqlite3")
    try:
        places = PlaceStore(store)
        with pytest.raises(Exception):
            places.replace_all([SavedPlace(place_id="p1", name="Nowhere")])
        # The failed replacement rolled back; the table is untouched.
        assert places.list() == []
    finally:
        store.close()
