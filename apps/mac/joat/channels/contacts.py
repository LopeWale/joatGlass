"""Resolve phone numbers and emails to contact names.

"+14052093077 · Messages" tells the wearer nothing at a glance; "Dele ·
Messages" tells them everything. Messages' own store only carries the raw
handle, so the name has to come from Contacts.

Read-only and immutable, like every other store this project touches. The
whole table is small (hundreds of rows), so it is loaded once and cached —
a per-notification query would be wasteful and would hold the file open.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import sqlite3

log = logging.getLogger(__name__)

_SOURCES = (
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb",
    "~/Library/Application Support/AddressBook/AddressBook-v22.abcddb",
)


def normalize_handle(handle: str) -> str:
    """Comparable form of a phone number; emails pass through lowercased.

    Numbers appear in Contacts as "(405) 209-3077" and in Messages as
    "+14052093077", so both collapse to digits. The last 10 are the
    comparable part — country codes are present in one source and absent
    from the other.
    """
    if "@" in handle:
        return handle.strip().lower()
    digits = re.sub(r"\D", "", handle)
    return digits[-10:] if len(digits) >= 10 else digits


def pretty_number(handle: str) -> str:
    """Fallback display when no contact matches: +1 (405) 209-3077."""
    digits = re.sub(r"\D", "", handle)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return handle


class ContactResolver:
    """handle -> display name, cached for the process lifetime."""

    def __init__(self) -> None:
        self._map: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pattern in _SOURCES:
            for path in glob.glob(os.path.expanduser(pattern)):
                try:
                    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
                except sqlite3.Error:
                    continue
                with conn:
                    for table, column in (("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                                          ("ZABCDEMAILADDRESS", "ZADDRESS")):
                        try:
                            rows = conn.execute(f"""
                                SELECT p.{column},
                                       COALESCE(r.ZFIRSTNAME, ''),
                                       COALESCE(r.ZLASTNAME, ''),
                                       COALESCE(r.ZORGANIZATION, '')
                                FROM {table} p
                                JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
                            """).fetchall()
                        except sqlite3.Error:
                            continue  # schema drift: skip this table, keep the rest
                        for value, first, last, org in rows:
                            if not value:
                                continue
                            name = " ".join(p for p in (first, last) if p) or org
                            if not name:
                                continue
                            key = normalize_handle(str(value))
                            if key:
                                out.setdefault(key, name)
        log.info("contacts loaded: %d handles", len(out))
        return out

    def name_for(self, handle: str) -> str:
        """A person's name when known, otherwise a readable number."""
        if not handle:
            return ""
        if self._map is None:
            try:
                self._map = self._load()
            except Exception:
                log.warning("contacts unavailable — showing raw handles")
                self._map = {}
        return self._map.get(normalize_handle(handle)) or pretty_number(handle)
