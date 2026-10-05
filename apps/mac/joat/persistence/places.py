"""The wearer's saved places, replaced whole from the phone page.

The phone page is the editor: every change sends the complete list, and
this store keeps the Mac's copy (`saved_places`, migration 14) so a route
to "home" resolves after a restart without the phone having to resend.
"""

from __future__ import annotations

from collections.abc import Sequence
import time

from ..core.protocol import PLACES_MAX, SavedPlace
from .store import DurableStore


class PlaceStore:
    """Read and atomically replace the saved places on the durable database."""

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def list(self) -> list[SavedPlace]:
        rows = self._store.execute(
            "SELECT place_id,name,address,latitude,longitude FROM saved_places "
            "ORDER BY position, place_id"
        ).fetchall()
        return [
            SavedPlace(
                place_id=place_id, name=name, address=address,
                latitude=latitude, longitude=longitude,
            )
            for place_id, name, address, latitude, longitude in rows
        ]

    def replace_all(self, places: Sequence[SavedPlace]) -> None:
        if len(places) > PLACES_MAX:
            raise ValueError("too many saved places")
        now = int(time.time() * 1000)
        with self._store.transaction() as db:
            db.execute("DELETE FROM saved_places")
            db.executemany(
                "INSERT INTO saved_places"
                "(place_id,position,name,address,latitude,longitude,updated_at_unix_ms) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        place.place_id, position, place.name, place.address,
                        place.latitude, place.longitude, now,
                    )
                    for position, place in enumerate(places)
                ],
            )
